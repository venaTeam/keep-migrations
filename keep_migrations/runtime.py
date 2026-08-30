"""Everything the migration entrypoint needs, and nothing else.

A leaf module by design: alembic, sqlalchemy and the standard library. It must
never import models, the serving engine, or anything under `src.repositories`.
This is the module that made the split possible: it is why the image needs
alembic, sqlalchemy and a driver rather than the gateway's 51 `src.*` modules
and their 366 transitive sqlalchemy/pydantic/fastapi/otel dependencies.

It builds its OWN engine rather than reusing the serving one. A Job wants a
single short-lived connection; the serving pool is sized for request traffic and
tuned with `pool_pre_ping` and a recycle window that mean nothing here.

The advisory-lock key MUST match keep-api-gateway's
`src/utils/cli/migration_runtime.py`, which still serialises pods that migrate
on startup. Two different defaults would mean the Job and a pod fail to
serialize against each other -- the exact collision the lock exists to prevent.
Change it in one place and you must change it in both.
"""

import logging
import os
import time
from contextlib import contextmanager

import alembic.config
from alembic.script import ScriptDirectory
from dotenv import find_dotenv, load_dotenv
from sqlalchemy import create_engine, text

# In the Job the connection string arrives from the secret via envFrom; locally
# it lives in .env, the same way `keep` picks it up.
load_dotenv(find_dotenv())

logger = logging.getLogger(__name__)

# Arbitrary, but must be identical in every process that migrates.
MIGRATION_LOCK_KEY = int(
    os.environ.get("KEEP_MIGRATION_ADVISORY_LOCK_KEY", "8274419300112233")
)
MIGRATION_LOCK_TIMEOUT = int(
    os.environ.get("KEEP_MIGRATION_LOCK_TIMEOUT_SECONDS", "3600")
)
MIGRATION_LOCK_POLL_SECONDS = float(
    os.environ.get("KEEP_MIGRATION_LOCK_POLL_SECONDS", "2")
)

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

_engine = None
_script_directory_cache: "ScriptDirectory | None" = None


def get_engine():
    """A connection to migrate through. Built once, lazily, with no pooling
    beyond the default -- a Job opens one connection and exits."""
    global _engine
    if _engine is None:
        url = os.environ.get("DATABASE_CONNECTION_STRING")
        if not url:
            raise RuntimeError("DATABASE_CONNECTION_STRING is not set")
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def get_alembic_config() -> "alembic.config.Config":
    """Alembic config with an absolute script_location, so the command works
    from any working directory."""
    cfg = alembic.config.Config(file_=os.path.join(_REPO_ROOT, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(_REPO_ROOT, "migrations"))
    return cfg


def script_directory() -> "ScriptDirectory | None":
    """This image's migration scripts. Memoised on success only -- caching a
    transient failure would wedge the process."""
    global _script_directory_cache
    if _script_directory_cache is not None:
        return _script_directory_cache
    try:
        _script_directory_cache = ScriptDirectory.from_config(get_alembic_config())
    except Exception:
        logger.exception("Failed to load the alembic script directory")
    return _script_directory_cache


def get_db_revision() -> str | None:
    """The revision stamped in the database, or None if `alembic_version` does
    not exist or is empty.

    Queried directly rather than via `inspect()`, which is a catalog scan.
    """
    try:
        with get_engine().connect() as conn:
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
    except Exception:
        logger.debug("Could not read alembic_version", exc_info=True)
        return None
    if not row:
        logger.info("alembic_version is empty; no stamped revision")
        return None
    return row[0]


@contextmanager
def migration_lock():
    """Serialize migrations with a Postgres advisory lock.

    The Job should be the only writer, but the lock still matters: a concurrent
    Argo sync, a retried Job, or a pod that still has SKIP_DB_CREATION unset
    would otherwise attempt the same DDL and fail with "already exists".

    No-op on non-Postgres dialects, where there are no concurrent writers.
    """
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        yield True
        return

    conn = engine.connect()
    try:
        # try + poll rather than the blocking pg_advisory_lock: `lock_timeout`
        # does not reliably bound advisory-lock waits.
        deadline = time.monotonic() + MIGRATION_LOCK_TIMEOUT
        while True:
            try:
                acquired = bool(
                    conn.execute(
                        text("SELECT pg_try_advisory_lock(:key)"),
                        {"key": MIGRATION_LOCK_KEY},
                    ).scalar()
                )
                conn.commit()
            except Exception:
                logger.warning(
                    "Error acquiring the migration advisory lock; proceeding without it",
                    exc_info=True,
                )
                yield False
                return
            if acquired:
                break
            if time.monotonic() >= deadline:
                logger.warning(
                    "Could not acquire the migration advisory lock within %ss; "
                    "proceeding without it",
                    MIGRATION_LOCK_TIMEOUT,
                )
                yield False
                return
            logger.info("Another process holds the migration lock; waiting")
            time.sleep(MIGRATION_LOCK_POLL_SECONDS)

        logger.info("Acquired migration advisory lock %s", MIGRATION_LOCK_KEY)
        try:
            yield True
        finally:
            try:
                conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": MIGRATION_LOCK_KEY},
                )
                conn.commit()
                logger.info("Released migration advisory lock %s", MIGRATION_LOCK_KEY)
            except Exception:
                # Session-scoped, so closing the connection releases it anyway.
                logger.warning("Failed to release the migration lock", exc_info=True)
    finally:
        conn.close()
