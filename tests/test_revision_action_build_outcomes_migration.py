"""`revision_action_build_outcomes`: adds the CI-webhook build outcomes.

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
    "2026-10-27-00-00_revision_action_build_outcomes.py",
)


def load_build_outcomes_migration(module_name="revision_action_build_outcomes_migration"):
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
    migration = load_build_outcomes_migration()
    assert migration.revision == "revision_action_build_outcomes"
    assert migration.down_revision == "suppression_reason_inactive"


def test_previous_values_match_the_creating_migration():
    """The downgrade rebuilds the type from PREVIOUS_VALUES — they must be exact."""
    migration = load_build_outcomes_migration()
    created = load_automation_migration("automation_tables_for_build_outcomes_test")
    assert tuple(created.revision_action.enums) == migration.PREVIOUS_VALUES
    assert not set(migration.ADDED_VALUES) & set(migration.PREVIOUS_VALUES)


def test_upgrade_adds_each_value_outside_a_transaction():
    migration = load_build_outcomes_migration()
    sql = _offline_postgres_sql(migration.upgrade)
    statements = [
        f"ALTER TYPE automation_revision_action ADD VALUE IF NOT EXISTS '{value}'"
        for value in ("build_succeeded", "build_failed")
    ]
    for statement in statements:
        assert statement in sql
        # autocommit_block ends the surrounding transaction first.
        assert sql.index("COMMIT") < sql.index(statement)


def test_downgrade_deletes_rows_then_rebuilds_the_type_in_order():
    migration = load_build_outcomes_migration()
    sql = _offline_postgres_sql(migration.downgrade)
    ordered = [
        "DELETE FROM automation_revisions WHERE action IN "
        "('build_succeeded', 'build_failed')",
        "ALTER TYPE automation_revision_action RENAME TO automation_revision_action_old",
        "CREATE TYPE automation_revision_action AS ENUM "
        "('create', 'edit', 'enable', 'disable', 'delete')",
        "ALTER TABLE automation_revisions ALTER COLUMN action TYPE "
        "automation_revision_action USING action::text::automation_revision_action",
        "DROP TYPE automation_revision_action_old",
    ]
    positions = [sql.index(statement) for statement in ordered]
    assert positions == sorted(positions)


def test_non_postgres_dialects_are_a_no_op():
    migration = load_build_outcomes_migration()
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        with Operations.context(context):
            migration.upgrade()
            migration.downgrade()
