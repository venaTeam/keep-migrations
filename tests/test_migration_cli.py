"""Tests for the migration entrypoint the Argo PreSync Job runs.

The hook fires on every sync, not every release, so the case that matters most is
the boring one: nothing pending, exit 0 without invoking alembic.
"""

import os
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from keep_migrations import cli as migrations


def _script(ancestry):
    """A ScriptDirectory stand-in. `ancestry` maps a revision to those it
    descends from, itself included; an absent revision is one this image's
    scripts have never heard of, which is what alembic raises on."""
    script = MagicMock()

    def iterate_revisions(upper, lower):
        if upper not in ancestry:
            raise Exception(f"Can't locate revision identified by '{upper}'")
        return [MagicMock(revision=rev) for rev in ancestry[upper]]

    def get_revision(target):
        resolved = "rev3" if target == "head" else target
        if resolved not in ancestry:
            return None
        return MagicMock(revision=resolved)

    script.iterate_revisions.side_effect = iterate_revisions
    script.get_revision.side_effect = get_revision
    return script


LINE = {"rev3": ["rev3", "rev2", "rev1"], "rev2": ["rev2", "rev1"], "rev1": ["rev1"]}


def _run(args, db_revision, ancestry=None, sql=""):
    with patch.object(
        migrations, "script_directory", return_value=_script(ancestry or LINE)
    ), patch.object(
        migrations, "get_db_revision", return_value=db_revision
    ), patch.object(
        migrations, "get_alembic_config", return_value=MagicMock()
    ), patch.object(
        migrations, "_offline_sql", return_value=sql
    ), patch.object(
        migrations, "migration_lock"
    ), patch(
        "alembic.command.upgrade"
    ) as upgrade, patch(
        "alembic.command.downgrade"
    ) as downgrade:
        result = CliRunner().invoke(migrations.main, args)
    return result, upgrade, downgrade


def test_nothing_pending_exits_zero_without_invoking_alembic():
    """The hook fires on every sync. A release with no new migrations must exit
    in seconds."""
    result, upgrade, downgrade = _run([], db_revision="rev3")
    assert result.exit_code == 0
    upgrade.assert_not_called()
    downgrade.assert_not_called()


def test_pending_revisions_upgrade_to_head():
    result, upgrade, _ = _run([], db_revision="rev1")
    assert result.exit_code == 0
    upgrade.assert_called_once()
    assert upgrade.call_args.args[1] == "rev3"


def test_fresh_database_upgrades():
    """No alembic_version yet: nothing to compare against."""
    result, upgrade, _ = _run([], db_revision=None)
    assert result.exit_code == 0
    upgrade.assert_called_once()


def test_downgrade_is_refused_without_allow_destructive():
    result, _, downgrade = _run(["--target", "rev1"], db_revision="rev3")
    assert result.exit_code != 0
    downgrade.assert_not_called()


def test_downgrade_runs_with_allow_destructive():
    result, _, downgrade = _run(
        ["--target", "rev1", "--allow-destructive"], db_revision="rev3"
    )
    assert result.exit_code == 0
    downgrade.assert_called_once()
    assert downgrade.call_args.args[1] == "rev1"


def test_revision_unknown_to_this_image_refuses_rather_than_guessing():
    """An image older than the database cannot reason about the stamped revision,
    and walking the schema somewhere nobody asked for is worse than failing the
    sync."""
    result, upgrade, downgrade = _run([], db_revision="rev9")
    assert result.exit_code != 0
    upgrade.assert_not_called()
    downgrade.assert_not_called()


def test_unknown_target_is_an_error():
    result, upgrade, _ = _run(["--target", "nope"], db_revision="rev1")
    assert result.exit_code != 0
    upgrade.assert_not_called()


def test_sql_prints_without_touching_the_database():
    result, upgrade, _ = _run(["--sql"], db_revision="rev1", sql="BEGIN;\nSELECT 1;\n")
    assert result.exit_code == 0
    assert "SELECT 1" in result.output
    upgrade.assert_not_called()


def test_check_passes_a_safe_path():
    result, upgrade, _ = _run(
        ["--check"], db_revision="rev1", sql="ALTER TABLE t ADD COLUMN c INT;"
    )
    assert result.exit_code == 0
    upgrade.assert_not_called()


def test_check_refuses_a_path_that_drops_data():
    result, _, _ = _run(
        ["--check", "--target", "rev1"],
        db_revision="rev3",
        sql="DROP TABLE preset;\nUPDATE alembic_version SET version_num='rev1';",
    )
    assert result.exit_code == migrations.EXIT_REFUSED


def test_check_refuses_a_downgrade_that_only_restamps():
    """17 of 122 migrations have an empty downgrade(): the path reports success
    while changing nothing."""
    result, _, _ = _run(
        ["--check", "--target", "rev1"],
        db_revision="rev3",
        sql="UPDATE alembic_version SET version_num='rev2';\n"
        "UPDATE alembic_version SET version_num='rev1';",
    )
    assert result.exit_code == migrations.EXIT_REFUSED


def test_allow_destructive_overrides_check():
    result, _, _ = _run(
        ["--check", "--target", "rev1", "--allow-destructive"],
        db_revision="rev3",
        sql="DROP TABLE preset;",
    )
    assert result.exit_code == 0


def test_converged_is_rechecked_under_the_lock():
    """Concurrent runs serialize on the advisory lock; the loser must notice the
    winner already converged rather than re-running the path."""
    revisions = iter(["rev1", "rev3"])  # behind before the lock, at head under it
    with patch.object(
        migrations, "script_directory", return_value=_script(LINE)
    ), patch.object(
        migrations, "get_db_revision", side_effect=lambda: next(revisions)
    ), patch.object(
        migrations, "get_alembic_config", return_value=MagicMock()
    ), patch.object(
        migrations, "migration_lock"
    ), patch(
        "alembic.command.upgrade"
    ) as upgrade:
        result = CliRunner().invoke(migrations.main, [])
    assert result.exit_code == 0
    upgrade.assert_not_called()


def _run_with_revisions(revisions, args=None, ancestry=None):
    """Like `_run`, but `get_db_revision` returns a different answer each call --
    the shape that matters once the lock can make us wait."""
    seq = iter(revisions)
    with patch.object(
        migrations, "script_directory", return_value=_script(ancestry or LINE)
    ), patch.object(
        migrations, "get_db_revision", side_effect=lambda: next(seq)
    ), patch.object(
        migrations, "get_alembic_config", return_value=MagicMock()
    ), patch.object(
        migrations, "migration_lock"
    ), patch(
        "alembic.command.upgrade"
    ) as upgrade, patch(
        "alembic.command.downgrade"
    ) as downgrade:
        result = CliRunner().invoke(migrations.main, args or [])
    return result, upgrade, downgrade


def test_direction_is_recalculated_under_the_lock():
    """Waiting for the lock can take as long as MIGRATION_LOCK_TIMEOUT, and
    whoever held it was migrating -- so the direction decided before the wait
    describes a database that no longer exists. Here the target is an upgrade
    before the wait and a downgrade after it: acting on the stale direction would
    upgrade a database that is already past the target."""
    result, upgrade, downgrade = _run_with_revisions(
        ["rev1", "rev3"], args=["--target", "rev2", "--allow-destructive"]
    )
    assert result.exit_code == 0
    upgrade.assert_not_called()
    downgrade.assert_called_once()


def test_the_downgrade_guard_is_reapplied_under_the_lock():
    """Same recalculation, without --allow-destructive: a direction that becomes
    a downgrade while we waited must be refused, not run."""
    result, upgrade, downgrade = _run_with_revisions(
        ["rev1", "rev3"], args=["--target", "rev2"]
    )
    assert result.exit_code == 1
    assert "--allow-destructive" in result.output
    upgrade.assert_not_called()
    downgrade.assert_not_called()


def test_lock_unavailable_fails_the_run():
    """The lock raises rather than letting a second run migrate alongside the
    first. It surfaces as a clean CLI error, not a traceback."""
    with patch.object(
        migrations, "script_directory", return_value=_script(LINE)
    ), patch.object(
        migrations, "get_db_revision", return_value="rev1"
    ), patch.object(
        migrations, "get_alembic_config", return_value=MagicMock()
    ), patch.object(
        migrations,
        "migration_lock",
        side_effect=migrations.LockUnavailable("another process holds it"),
    ), patch(
        "alembic.command.upgrade"
    ) as upgrade:
        result = CliRunner().invoke(migrations.main, [])
    assert result.exit_code == 1
    assert "another process holds it" in result.output
    upgrade.assert_not_called()


def test_destructive_reason_ignores_sql_comments():
    """Alembic's offline output is mostly `-- Running upgrade ...` comment lines;
    a no-op downgrade must still be recognised through them."""
    sql = (
        "-- Running downgrade rev3 -> rev2\n"
        "UPDATE alembic_version SET version_num='rev2';\n"
        "-- Running downgrade rev2 -> rev1\n"
        "UPDATE alembic_version SET version_num='rev1';\n"
    )
    assert migrations._destructive_reason(sql) is not None
    assert migrations._destructive_reason("ALTER TABLE t ADD COLUMN c INT;") is None


def test_dropping_a_constraint_is_destructive():
    """No data leaves, but the guarantee protecting it does: duplicates can be
    written afterwards, and re-applying the migration then fails. One real
    downgrade path (preset_tag_name_per_tenant) drops a unique constraint and
    nothing else -- it passed as safe before this marker."""
    sql = "ALTER TABLE tag DROP CONSTRAINT uq_tag_tenant_name;"
    assert "DROP CONSTRAINT" in migrations._destructive_reason(sql)


def test_truncate_and_drop_schema_are_destructive():
    """Alembic never emits either; a hand-written op.execute() can, and they are
    the cases you cannot undo."""
    assert migrations._destructive_reason("TRUNCATE TABLE alert;") is not None
    assert migrations._destructive_reason("DROP SCHEMA public CASCADE;") is not None


def test_delete_from_a_real_table_is_destructive():
    assert migrations._destructive_reason("DELETE FROM alert WHERE id > 5;") is not None


def test_delete_from_alembic_version_alone_is_not_destructive():
    """Every downgrade restamps by deleting from alembic_version. Treating that
    as data loss would refuse every path there is."""
    sql = (
        "ALTER TABLE alert ADD COLUMN note VARCHAR;\n"
        "DELETE FROM alembic_version WHERE alembic_version.version_num = 'rev3';\n"
        "INSERT INTO alembic_version (version_num) VALUES ('rev2');"
    )
    assert migrations._destructive_reason(sql) is None


def test_a_marker_inside_a_comment_is_not_destructive():
    """Alembic's offline output quotes each migration's docstring in a comment.
    Scanning those refuses a path on the strength of a sentence someone wrote
    about a migration rather than the SQL it emits."""
    sql = (
        "-- Running downgrade rev3 -> rev2, drop table alertenrichment (SC-05)\n"
        "ALTER TABLE alert ADD COLUMN note VARCHAR;"
    )
    assert migrations._destructive_reason(sql) is None


def test_dropping_an_index_is_not_destructive():
    """Deliberately absent from the markers: no data is lost, and it appears in
    four otherwise-safe downgrade paths. A guard that fires on safe things gets
    --allow-destructive pasted onto every command, and then it guards nothing."""
    assert migrations._destructive_reason("DROP INDEX ix_alert_tenant_id;") is None


def test_offline_span_for_a_fresh_database_walks_from_base():
    """An empty start ident is not valid -- alembic asserts on it -- so a
    database with no alembic_version must resolve to "base"."""
    config = MagicMock()
    with patch("alembic.command.upgrade") as upgrade:
        migrations._offline_sql(config, None, "rev3", "upgrade")
    assert upgrade.call_args.args[1] == "base:rev3"

    with patch("alembic.command.upgrade") as upgrade:
        migrations._offline_sql(config, "rev1", "rev3", "upgrade")
    assert upgrade.call_args.args[1] == "rev1:rev3"
