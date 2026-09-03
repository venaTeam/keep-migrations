"""The migration entrypoint the Argo PreSync Job runs.

This repo exists so that no serving image contains migration code, and so the
schema has one owner rather than a docstring saying it does. Nothing imports
this package: it ships as an image, not a library.

Direction is derived, never commanded: the target is compared to the revision
stamped in `alembic_version` and the walk goes whichever way the graph says.
The default target is `head` and nothing exists past head, so a normal release
can never downgrade.

Exit codes: 0 converged (including nothing to do), 1 refused, 2 failed.
"""

import logging
import os
import sys
from io import StringIO

import alembic.command
import click
from alembic.script import ScriptDirectory

from keep_migrations.direction import Direction
from keep_migrations.runtime import (
    LockUnavailable,
    get_alembic_config,
    get_db_revision,
    migration_lock,
    script_directory,
)

logger = logging.getLogger(__name__)

EXIT_OK = 0
EXIT_REFUSED = 1

# A downgrade path that only restamps is one whose migrations never wrote a real
# `downgrade()` -- it reports success while changing nothing. Matched against the
# SQL upper-cased.
_NO_OP_MARKER = "UPDATE ALEMBIC_VERSION"

# Matched against the SQL a path WOULD emit, upper-cased, comments removed. The
# point is not to make a destructive path impossible -- `--allow-destructive`
# always overrides -- but to make it deliberate, so dropping a column of
# production data is something you typed rather than something that happened
# while rolling back a release you assumed was harmless.
#
# DROP INDEX is deliberately absent. It loses no data, and it appears in 4
# downgrade paths that are otherwise entirely safe; refusing those would train
# everyone to paste --allow-destructive onto every command, and then this guards
# nothing. TRUNCATE, DELETE FROM and DROP SCHEMA never appear in the revisions
# today -- alembic does not emit them -- but a hand-written `op.execute()` can,
# and they are the cases you cannot undo.
_DESTRUCTIVE_MARKERS = (
    "DROP TABLE",
    "DROP COLUMN",
    # Data stays, but the guarantee protecting it does not: once a unique
    # constraint is gone duplicates can be written, and re-applying the migration
    # later fails if any crept in. A one-way door dressed as a reversible change.
    "DROP CONSTRAINT",
    "DROP SCHEMA",
    "TRUNCATE",
)

#: `DELETE FROM alembic_version` is bookkeeping in every downgrade, so this one
#: cannot be a plain substring like the rest.
_DELETE_MARKER = "DELETE FROM"
_DELETE_BOOKKEEPING = "DELETE FROM ALEMBIC_VERSION"


def _resolve(script: ScriptDirectory, target: str) -> str:
    """The concrete revision `target` names ('head' -> a hash)."""
    try:
        revision = script.get_revision(target)
    except Exception as exc:
        raise click.ClickException(f"Cannot resolve target '{target}': {exc}")
    if revision is None:
        raise click.ClickException(f"Target revision '{target}' is not in this image")
    return revision.revision


def _is_ancestor(script: ScriptDirectory, candidate: str, descendant: str) -> bool:
    """True if `candidate` is `descendant` or one of its ancestors."""
    try:
        return any(
            r.revision == candidate for r in script.iterate_revisions(descendant, "base")
        )
    except Exception:
        return False


def _direction(
    script: ScriptDirectory, db_revision: str | None, target: str
) -> Direction:
    """Which direction converges the database on `target`.

    A database stamped with a revision this image has never heard of is not a
    direction -- it is a target the image cannot reason about, and guessing would
    walk the schema somewhere nobody asked for.
    """
    if db_revision is None:
        # Fresh database: no alembic_version, nothing to compare.
        return Direction.UPGRADE
    if db_revision == target:
        return Direction.CONVERGED
    if _is_ancestor(script, db_revision, target):
        return Direction.UPGRADE
    if _is_ancestor(script, target, db_revision):
        return Direction.DOWNGRADE
    raise click.ClickException(
        f"Database revision '{db_revision}' and target '{target}' are on divergent "
        "branches, or the database was stamped by an image newer than this one. "
        "Refusing to guess a direction."
    )


def _offline_sql(
    config, db_revision: str | None, target: str, direction: Direction
) -> str:
    """The SQL the run would emit, without touching the database.

    Alembic writes offline SQL to stdout, so capture it rather than plumb a buffer
    through the config.
    """
    buffer = StringIO()
    original = sys.stdout
    # `--sql` needs an explicit start:end range. An empty start is not a valid
    # ident -- alembic asserts on it -- so a fresh database walks from "base".
    span = f"{db_revision or 'base'}:{target}"
    try:
        sys.stdout = buffer
        if direction is Direction.DOWNGRADE:
            alembic.command.downgrade(config, span, sql=True)
        else:
            alembic.command.upgrade(config, span, sql=True)
    finally:
        sys.stdout = original
    return buffer.getvalue()


def _statements(sql: str) -> list[str]:
    """The executable lines, with alembic's comments dropped.

    Its offline output is mostly `-- Running downgrade X -> Y, <docstring>`, and
    those docstrings describe what the migration does -- so scanning them for
    "DROP TABLE" refuses paths on the strength of a sentence someone wrote about
    a migration rather than the SQL it emits.
    """
    return [
        line.strip()
        for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]


def _destructive_reason(sql: str) -> str | None:
    """Why this path should not run unattended, or None if it is safe."""
    statements = _statements(sql)
    upper = "\n".join(statements).upper()

    hits = [m for m in _DESTRUCTIVE_MARKERS if m in upper]
    if _DELETE_MARKER in upper and any(
        _DELETE_MARKER in s.upper() and _DELETE_BOOKKEEPING not in s.upper()
        for s in statements
    ):
        hits.append(_DELETE_MARKER)
    if hits:
        return f"the path emits {', '.join(hits)} -- data leaves the database"

    if statements and all(_NO_OP_MARKER in s.upper() for s in statements):
        return (
            "the path only restamps alembic_version -- every migration crossed has "
            "an empty downgrade(), so this reports success while changing nothing"
        )
    return None


def _refuse_unguarded_downgrade(direction: Direction, allow_destructive: bool) -> None:
    """A downgrade needs `--allow-destructive`. Checked before taking the lock
    and again under it, since the direction can change while waiting."""
    if direction is Direction.DOWNGRADE and not allow_destructive:
        raise click.ClickException(
            "Refusing to downgrade without --allow-destructive. Downgrade one "
            "release, not many: long paths break the single-transaction guarantee "
            "and most downgrade() functions have never run anywhere."
        )


@click.command()
@click.option(
    "--target", default="head", show_default=True, help="Revision to converge on."
)
@click.option(
    "--sql", "as_sql", is_flag=True, help="Print the SQL and exit; touches nothing."
)
@click.option(
    "--check", is_flag=True, help="Resolve the path and refuse if destructive."
)
@click.option(
    "--allow-destructive",
    is_flag=True,
    help="Run a path --check would refuse. Required for any downgrade.",
)
def main(target: str, as_sql: bool, check: bool, allow_destructive: bool) -> None:
    """Converge the database on TARGET, in whichever direction the graph says."""
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    config = get_alembic_config()
    script = script_directory()
    if script is None:
        raise click.ClickException("Could not load this image's migration scripts")

    target_revision = _resolve(script, target)
    db_revision = get_db_revision()
    direction = _direction(script, db_revision, target_revision)

    logger.info(
        "database=%s target=%s (%s) direction=%s",
        db_revision or "<empty>",
        target,
        target_revision,
        direction,
    )

    # The hook fires on every sync, not every release, so the common case is
    # nothing pending: exit without invoking alembic at all.
    if direction is Direction.CONVERGED:
        logger.info("Database is already at the target; nothing to do")
        sys.exit(EXIT_OK)

    if as_sql or check:
        sql = _offline_sql(config, db_revision, target_revision, direction)
        if as_sql:
            click.echo(sql)
        if check:
            reason = _destructive_reason(sql)
            if reason:
                logger.error("Refusing the %s: %s", direction, reason)
                sys.exit(EXIT_OK if allow_destructive else EXIT_REFUSED)
            logger.info("Path is safe to run unattended")
        sys.exit(EXIT_OK)

    _refuse_unguarded_downgrade(direction, allow_destructive)

    try:
        _converge(config, script, target_revision, allow_destructive)
    except LockUnavailable as exc:
        # Raised on __enter__, so this has to wrap the `with`, not the call.
        raise click.ClickException(str(exc))

    logger.info("Database converged on %s", target_revision)
    sys.exit(EXIT_OK)


def _converge(config, script, target_revision: str, allow_destructive: bool) -> None:
    """Take the lock, re-decide, and walk. Split out so the LockUnavailable
    handler above wraps the whole `with`, including its __enter__."""
    with migration_lock():
        # Re-read AND re-decide under the lock. Waiting for it can take as long
        # as MIGRATION_LOCK_TIMEOUT, and whoever held it was migrating -- so the
        # direction computed before the wait describes a database that no longer
        # exists. Acting on it could walk the schema the wrong way.
        db_revision = get_db_revision()
        direction = _direction(script, db_revision, target_revision)
        if direction is Direction.CONVERGED:
            logger.info("Another run reached the target while we waited; nothing to do")
            sys.exit(EXIT_OK)
        # The guard is re-applied for the same reason: a direction that was an
        # upgrade before the wait can be a downgrade after it.
        _refuse_unguarded_downgrade(direction, allow_destructive)
        logger.info("Direction under the lock: %s (database=%s)", direction, db_revision)

        if direction is Direction.DOWNGRADE:
            alembic.command.downgrade(config, target_revision)
        else:
            alembic.command.upgrade(config, target_revision)


if __name__ == "__main__":
    main()
