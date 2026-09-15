"""`automation_suppression_reason_inactive`: adds `inactive` to the enum.

Hermetic like the other migration tests: the module is loaded by path and driven
through alembic's ``MigrationContext`` + ``Operations``. Postgres output is
checked in offline (``as_sql``) mode — that is the exact DDL the Job emits —
and the non-Postgres path is executed against SQLite to prove it is a no-op.
"""

import importlib.util
import io
import os

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.test_create_automation_tables_migration import load_automation_migration

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "migrations",
    "versions",
    "2026-09-15-00-00_automation_suppression_reason_inactive.py",
)


def load_inactive_migration(module_name="suppression_reason_inactive_migration"):
    spec = importlib.util.spec_from_file_location(module_name, MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _offline_postgres_sql(step) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(context):
        step()
    return buffer.getvalue()


def test_revision_chain_follows_the_current_head():
    migration = load_inactive_migration()
    assert migration.revision == "automation_suppression_reason_inactive"
    assert migration.down_revision == "merge_automations_preset_tag"


def test_previous_values_match_the_creating_migration():
    """The downgrade rebuilds the type from PREVIOUS_VALUES — they must be exact."""
    migration = load_inactive_migration()
    created = load_automation_migration("automation_tables_for_inactive_test")
    assert tuple(created.suppression_reason.enums) == migration.PREVIOUS_VALUES
    assert migration.ADDED_VALUE not in migration.PREVIOUS_VALUES


def test_upgrade_adds_the_value_outside_a_transaction():
    migration = load_inactive_migration()
    sql = _offline_postgres_sql(migration.upgrade)
    statement = "ALTER TYPE automation_suppression_reason ADD VALUE IF NOT EXISTS 'inactive'"
    assert statement in sql
    # autocommit_block ends the surrounding transaction first.
    assert sql.index("COMMIT") < sql.index(statement)


def test_downgrade_clears_rows_then_rebuilds_the_type_in_order():
    migration = load_inactive_migration()
    sql = _offline_postgres_sql(migration.downgrade)
    ordered = [
        "UPDATE automation_runs SET suppression_reason = NULL "
        "WHERE suppression_reason = 'inactive'",
        "ALTER TYPE automation_suppression_reason RENAME TO "
        "automation_suppression_reason_old",
        "CREATE TYPE automation_suppression_reason AS ENUM ('duplicate', 'cooldown')",
        "ALTER TABLE automation_runs ALTER COLUMN suppression_reason TYPE "
        "automation_suppression_reason USING "
        "suppression_reason::text::automation_suppression_reason",
        "DROP TYPE automation_suppression_reason_old",
    ]
    positions = [sql.index(statement) for statement in ordered]
    assert positions == sorted(positions)


def test_non_postgres_dialects_are_a_no_op():
    migration = load_inactive_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.downgrade()
