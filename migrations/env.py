"""Alembic environment.

MIGRATIONS HERE ARE HAND-WRITTEN. `--autogenerate` is not used and is not
supported: the last generated revision was 2026-05-12 (`e1932c411f61`), and
every one of the 20+ since has been written by hand.

That is why this module imports no models. `target_metadata` and
`include_object` are consulted *only* by autogenerate -- an `upgrade` or
`downgrade` run just executes the scripts -- so wiring them up bought nothing
and cost something real: the import list had drifted to 18 modules against 44
declared tables, leaving five (`enrichmentevent`, `enrichmentlog`,
`externalaiconfigandmetadata`, `providerimage`, `system`) present in the
database but absent from the metadata. To autogenerate, a table in the database
and not in `target_metadata` is a table to delete, so a stray `--autogenerate`
would have emitted `op.drop_table()` for all five.

Without `target_metadata` there is nothing to diff against, so that cannot
happen. Schema/model drift is caught instead by keep-api-gateway's
`db_on_start.schema_drift()`, which runs behind `/readyz` against the live
database and refuses to start a pod whose schema is missing something.
"""

import asyncio
import logging
import os
from logging.config import fileConfig

from alembic import context
from alembic.script import ScriptDirectory
from sqlalchemy.future import Connection

from keep_migrations.runtime import get_engine

# `list_migrations` logs through this on the failure path. It was never defined
# in the gateway's copy, so a failure inside the diagnostic raised NameError and
# buried the migration error that triggered it.
logger = logging.getLogger(__name__)

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config


# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    # backup the current config
    logging_config = config.get_section("loggers")
    fileConfig(config.config_file_name)


async def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    This configures the context with just a URL
    and not an Engine, though an Engine is acceptable
    here as well.  By skipping the Engine creation
    we don't even need a DBAPI to be available.

    Calls to context.execute() here emit the given string to the
    script output.

    """
    connectable = get_engine()
    context.configure(
        url=str(connectable.url),
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Run actual sync migrations.

    :param connection: connection to the database.
    """
    context.configure(
        connection=connection,
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    In this scenario we need to create an Engine
    and associate a connection with the context.
    """
    connectable = get_engine()
    try:
        do_run_migrations(connectable.connect())
    except Exception as e:
        # print all migrations so we will know what failed
        list_migrations(connectable)
        raise e


def list_migrations(connectable):
    """
    List all migrations and their status for debugging.
    """
    try:
        # Get the script directory from the alembic context
        script_directory = ScriptDirectory.from_config(config)
        current_rev = script_directory.get_current_head()
        # List all available migrations
        pid = os.getpid()
        print(f"[{pid}] Available migrations:")
        try:
            for script in script_directory.walk_revisions():
                status = (
                    "PENDING"
                    if current_rev and script.revision > current_rev
                    else "APPLIED"
                )
                print(f"  - {script.revision}: {script.doc} ({status})")
        except Exception as exc:
            logger.exception(f"Failed to list migrations: {exc}")
    except Exception as exc:
        logger.exception(f"Failed to process migration information: {exc}")


loop = asyncio.get_event_loop()
if context.is_offline_mode():
    task = run_migrations_offline()
else:
    task = run_migrations_online()

loop.run_until_complete(task)

