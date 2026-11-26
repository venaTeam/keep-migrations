"""Coverage for the `create_assistant_tables` revision.

Loads the revision by path and drives its real upgrade()/downgrade() through
alembic's MigrationContext, as the other tests/test_*_migration.py files do:

  - against SQLite, to prove the DDL executes in both directions. DDL only: no
    rows are inserted, because `assistant_message.seq` is an identity column on
    Postgres and a plain NOT NULL column on SQLite, which has no identity;
  - offline (``as_sql``) with the PostgreSQL dialect, which is the exact DDL the
    Job emits. Everything SQLite ignores (identity, JSONB, timestamptz, partial
    index predicates, lock_timeout) is asserted there.
"""

import importlib.util
import io
import os
import re

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from keep_migrations import runtime
from keep_migrations.cli import _destructive_reason

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "migrations",
    "versions",
    "2026-11-24-00-00_create_assistant_tables.py",
)

# Creation order; the downgrade must use the reverse.
TABLES = ("assistant_conversation", "assistant_draft", "assistant_message")
CHILD_TABLES = ("assistant_draft", "assistant_message")

# Everything below is spelled out independently of the module under test.
# Column name -> nullable.
EXPECTED_COLUMNS = {
    "assistant_conversation": {
        "id": False,
        "tenant_id": False,
        "created_by": False,
        "title": True,
        "active_turn_id": True,
        "turn_started_at": True,
        "created_at": False,
        "updated_at": False,
    },
    "assistant_draft": {
        "id": False,
        "tenant_id": False,
        "conversation_id": False,
        "created_by": False,
        "kind": False,
        "state": False,
        "permitted": False,
        "payload": False,
        "outcome": True,
        "expires_at": False,
        "resolved_at": True,
        "created_at": False,
        "updated_at": False,
    },
    "assistant_message": {
        "id": False,
        "tenant_id": False,
        "conversation_id": False,
        "seq": False,
        "role": False,
        "status": False,
        "content": True,
        "tool_calls": True,
        "tool_call_id": True,
        "draft_id": True,
        "model": True,
        "prompt_tokens": True,
        "completion_tokens": True,
        "created_at": False,
    },
}

EXPECTED_INDEXES = {
    "assistant_conversation": {
        "ix_assistant_conversation_owner": ["tenant_id", "created_by", "updated_at"],
        "ix_assistant_conversation_updated_at": ["updated_at"],
    },
    "assistant_draft": {
        "ix_assistant_draft_conversation": ["conversation_id", "created_at"],
        "ix_assistant_draft_pending_expiry": ["expires_at"],
    },
    "assistant_message": {
        "ix_assistant_message_conversation": ["conversation_id", "seq"],
        "ix_assistant_message_draft": ["draft_id"],
        "ix_assistant_message_user_rate": ["conversation_id", "created_at"],
    },
}

EXPECTED_CHECKS = {
    "ck_assistant_draft_state": (
        "state IN ('pending', 'applying', 'applied', 'partially_applied', "
        "'failed', 'discarded', 'expired')"
    ),
    "ck_assistant_message_role": "role IN ('user', 'assistant', 'tool', 'event')",
    "ck_assistant_message_status": "status IN ('complete', 'interrupted', 'error')",
}


def load_assistant_migration(module_name="create_assistant_tables_migration"):
    """Import the revision module by path."""
    spec = importlib.util.spec_from_file_location(module_name, MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            step()


def _offline_postgres_sql(step) -> str:
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer},
    )
    with Operations.context(context):
        step()
    return buffer.getvalue()


def _flat(sql: str) -> str:
    """Whitespace collapsed, so assertions do not depend on layout."""
    return re.sub(r"\s+", " ", sql)


def _statements(flat_sql: str) -> list[str]:
    return [s.strip() for s in flat_sql.split(";") if s.strip()]


def _create_table_sql(flat_sql: str, table: str) -> str:
    prefix = f"CREATE TABLE {table} ("
    return next(s for s in _statements(flat_sql) if s.startswith(prefix))


@pytest.fixture(scope="module")
def migration():
    return load_assistant_migration()


@pytest.fixture(scope="module")
def upgrade_sql(migration):
    return _flat(_offline_postgres_sql(migration.upgrade))


@pytest.fixture(scope="module")
def downgrade_sql(migration):
    return _flat(_offline_postgres_sql(migration.downgrade))


@pytest.fixture
def engine(tmp_path):
    """Only the FK target (`tenant`) exists, as the revision expects."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'assistant.db'}")
    md = sa.MetaData()
    sa.Table("tenant", md, sa.Column("id", sa.String(), primary_key=True))
    md.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def migrated_engine(engine, migration):
    _run(engine, migration.upgrade)
    return engine


# --- lineage ---------------------------------------------------------------


def test_revision_chain(migration):
    assert migration.revision == "create_assistant_tables"
    assert migration.down_revision == "lastalert_window_tags"


def test_revision_is_in_the_shipped_lineage(migration):
    """The file is where alembic finds it. That there is exactly one head is
    tests/test_migration_lock.py's job; which head it is changes with every
    later revision, so it is not pinned here."""
    script = runtime.script_directory().get_revision("create_assistant_tables")
    assert script.down_revision == migration.down_revision


def test_revision_id_fits_alembic_version(migration):
    # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite does not
    # enforce it, so a longer id only fails when stamped on a real database.
    assert len(migration.revision) <= 32


# --- SQLite execution ------------------------------------------------------


def test_upgrade_creates_exactly_the_three_tables(migrated_engine):
    created = set(sa.inspect(migrated_engine).get_table_names()) - {"tenant"}
    assert created == set(TABLES)


@pytest.mark.parametrize("table", TABLES)
def test_columns_and_nullability(migrated_engine, table):
    columns = {
        c["name"]: c["nullable"] for c in sa.inspect(migrated_engine).get_columns(table)
    }
    assert columns == EXPECTED_COLUMNS[table]


@pytest.mark.parametrize("table", TABLES)
def test_primary_key_is_id(migrated_engine, table):
    pk = sa.inspect(migrated_engine).get_pk_constraint(table)
    assert pk["constrained_columns"] == ["id"]


@pytest.mark.parametrize("table", TABLES)
def test_indexes(migrated_engine, table):
    indexes = {
        ix["name"]: ix["column_names"]
        for ix in sa.inspect(migrated_engine).get_indexes(table)
    }
    assert indexes == EXPECTED_INDEXES[table]


@pytest.mark.parametrize("table", TABLES)
def test_tenant_id_fks_to_tenant(migrated_engine, table):
    fks = [
        fk
        for fk in sa.inspect(migrated_engine).get_foreign_keys(table)
        if fk["constrained_columns"] == ["tenant_id"]
    ]
    assert len(fks) == 1, f"{table}.tenant_id has no single FK: {fks}"
    assert fks[0]["referred_table"] == "tenant"
    assert fks[0]["referred_columns"] == ["id"]
    assert not fks[0]["options"]


def test_conversation_exposes_the_composite_fk_target(migrated_engine):
    uniques = sa.inspect(migrated_engine).get_unique_constraints(
        "assistant_conversation"
    )
    assert [(u["name"], u["column_names"]) for u in uniques] == [
        ("uq_assistant_conversation_id_tenant_id", ["id", "tenant_id"])
    ]


@pytest.mark.parametrize("table", CHILD_TABLES)
def test_child_tenant_is_pinned_to_the_conversation(migrated_engine, table):
    """`(conversation_id, tenant_id)` must exist as a pair on the conversation,
    so a child row cannot carry another tenant's id. A plain
    `conversation_id -> id` FK would leave such a row writable."""
    fks = [
        fk
        for fk in sa.inspect(migrated_engine).get_foreign_keys(table)
        if "conversation_id" in fk["constrained_columns"]
    ]
    assert len(fks) == 1, fks
    assert fks[0]["constrained_columns"] == ["conversation_id", "tenant_id"]
    assert fks[0]["referred_table"] == "assistant_conversation"
    assert fks[0]["referred_columns"] == ["id", "tenant_id"]
    assert not fks[0]["options"], "deleting a conversation must not cascade"


def test_draft_id_is_set_null_when_the_draft_goes(migrated_engine):
    fks = [
        fk
        for fk in sa.inspect(migrated_engine).get_foreign_keys("assistant_message")
        if fk["constrained_columns"] == ["draft_id"]
    ]
    assert len(fks) == 1, fks
    assert fks[0]["referred_table"] == "assistant_draft"
    assert fks[0]["referred_columns"] == ["id"]
    assert fks[0]["options"] == {"ondelete": "SET NULL"}


def test_downgrade_drops_every_table_and_upgrade_runs_again(
    migrated_engine, migration
):
    _run(migrated_engine, migration.downgrade)
    assert set(sa.inspect(migrated_engine).get_table_names()) == {"tenant"}
    _run(migrated_engine, migration.upgrade)
    assert set(TABLES) <= set(sa.inspect(migrated_engine).get_table_names())


# --- Postgres DDL ----------------------------------------------------------


def test_postgres_creates_parents_before_children(upgrade_sql):
    created = [
        s.split()[2] for s in _statements(upgrade_sql) if s.startswith("CREATE TABLE")
    ]
    assert created == list(TABLES)


def test_postgres_only_creates_assistant_objects(upgrade_sql):
    """Nothing here may touch an existing table beyond the FK to `tenant`."""
    for statement in _statements(upgrade_sql):
        assert statement.startswith(
            ("CREATE TABLE assistant_", "CREATE INDEX ix_assistant_")
        )


def test_postgres_seq_is_a_bigint_identity(upgrade_sql):
    """`now()` is constant inside a transaction, so only `seq` can order the
    rows of one tool-call group."""
    assert "seq BIGINT GENERATED BY DEFAULT AS IDENTITY" in _create_table_sql(
        upgrade_sql, "assistant_message"
    )
    assert upgrade_sql.count("AS IDENTITY") == 1


@pytest.mark.parametrize("name,predicate", sorted(EXPECTED_CHECKS.items()))
def test_postgres_checks_are_named(upgrade_sql, name, predicate):
    assert f"CONSTRAINT {name} CHECK ({predicate})" in upgrade_sql


def test_postgres_has_no_other_check(upgrade_sql):
    assert upgrade_sql.count("CHECK (") == len(EXPECTED_CHECKS)


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE INDEX ix_assistant_draft_pending_expiry ON assistant_draft "
        "(expires_at) WHERE state = 'pending'",
        "CREATE INDEX ix_assistant_message_draft ON assistant_message "
        "(draft_id) WHERE draft_id IS NOT NULL",
        "CREATE INDEX ix_assistant_message_user_rate ON assistant_message "
        "(conversation_id, created_at) WHERE role = 'user'",
    ],
)
def test_postgres_partial_indexes(upgrade_sql, ddl):
    assert ddl in _statements(upgrade_sql)


def test_postgres_has_no_other_partial_index(upgrade_sql):
    assert upgrade_sql.count(" WHERE ") == 3


def test_postgres_foreign_keys(upgrade_sql):
    for table in TABLES:
        assert "FOREIGN KEY(tenant_id) REFERENCES tenant (id)" in _create_table_sql(
            upgrade_sql, table
        )
    for table in CHILD_TABLES:
        assert (
            "FOREIGN KEY(conversation_id, tenant_id) "
            "REFERENCES assistant_conversation (id, tenant_id)"
        ) in _create_table_sql(upgrade_sql, table)
    assert upgrade_sql.count("ON DELETE") == 1
    assert (
        "FOREIGN KEY(draft_id) REFERENCES assistant_draft (id) ON DELETE SET NULL"
        in upgrade_sql
    )


def test_postgres_timestamps_are_timestamptz(upgrade_sql):
    # 3 on conversation, 4 on draft, 1 on message.
    assert upgrade_sql.count("TIMESTAMP WITH TIME ZONE") == 8
    assert "TIMESTAMP WITHOUT TIME ZONE" not in upgrade_sql


@pytest.mark.parametrize(
    "table,columns",
    [
        ("assistant_conversation", ("created_at", "updated_at")),
        ("assistant_draft", ("created_at", "updated_at")),
        ("assistant_message", ("created_at",)),
    ],
)
def test_postgres_row_timestamps_default_to_now(upgrade_sql, table, columns):
    ddl = _create_table_sql(upgrade_sql, table)
    for column in columns:
        assert f"{column} TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL" in ddl


def test_postgres_draft_expiry_has_no_default(upgrade_sql):
    """Every insert states its TTL; a default would hide a forgotten one."""
    assert "expires_at TIMESTAMP WITH TIME ZONE NOT NULL" in upgrade_sql


def test_postgres_defaults(upgrade_sql):
    draft = _create_table_sql(upgrade_sql, "assistant_draft")
    assert "state TEXT DEFAULT 'pending' NOT NULL" in draft
    assert "permitted BOOLEAN DEFAULT true NOT NULL" in draft
    message = _create_table_sql(upgrade_sql, "assistant_message")
    assert "status TEXT DEFAULT 'complete' NOT NULL" in message


def test_postgres_json_columns_are_jsonb(upgrade_sql):
    assert "payload JSONB NOT NULL" in upgrade_sql
    assert "outcome JSONB," in upgrade_sql
    assert "tool_calls JSONB," in upgrade_sql
    assert upgrade_sql.count("JSON") == 3


def test_upgrade_path_is_safe_to_run_unattended(migration):
    assert _destructive_reason(_offline_postgres_sql(migration.upgrade)) is None


# --- Postgres downgrade ----------------------------------------------------


def test_postgres_downgrade_bounds_its_lock_wait_before_dropping(downgrade_sql):
    """Dropping a referencing table takes ACCESS EXCLUSIVE on `tenant`, which
    every service reads; an unbounded wait would queue all of them behind it."""
    assert _statements(downgrade_sql) == [
        "SET LOCAL lock_timeout = '5s'",
        "DROP TABLE assistant_message",
        "DROP TABLE assistant_draft",
        "DROP TABLE assistant_conversation",
    ]


def test_postgres_downgrade_needs_allow_destructive(migration):
    assert _destructive_reason(_offline_postgres_sql(migration.downgrade))
