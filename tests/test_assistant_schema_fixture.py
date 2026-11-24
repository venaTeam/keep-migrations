"""The assistant DDL in this repo must match the golden schema fixture.

The migration that creates the assistant tables lives here, the ORM models that
use them live in keep-assistant, and nothing runs both.
``tests/fixtures/assistant_schema.json`` is the shared reference: vendored
byte-identically into keep-assistant, which asserts its models against its own
copy. Same arrangement as tests/test_automation_schema_fixture.py, which
explains why the declared schema objects are compared (PostgreSQL dialect,
offline) rather than a reflected SQLite database.

Besides the comparison, this file checks the fixture itself (a regenerated
golden that lost the identity on ``seq`` would still equal a migration that lost
it) and checks that the helper reads Postgres' reflected spelling of this schema
back to the same normal form, which is what keep-assistant relies on.
"""

import io
import json
import os
from types import SimpleNamespace

import alembic.op as alembic_op
import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql

from tests.fixtures.assistant_schema_dump import (
    ASSISTANT_TABLES,
    _canonical_default,
    _canonical_predicate,
    _check_entry,
    _foreign_key_entry,
    dump_from_inspector,
    dump_from_tables,
)
from tests.test_create_assistant_tables_migration import load_assistant_migration

GOLDEN_PATH = os.path.join(
    os.path.dirname(__file__), "fixtures", "assistant_schema.json"
)

with open(GOLDEN_PATH, encoding="utf-8") as _fh:
    GOLDEN = json.load(_fh)["tables"]

SECTIONS = (
    "columns",
    "primary_key",
    "indexes",
    "unique_constraints",
    "foreign_keys",
    "check_constraints",
)


def _record_declared_schema(migration):
    """Run ``upgrade()`` for its declarations, offline, under PostgreSQL.

    ``op.create_table`` returns the ``sa.Table`` it builds. ``op.create_index``
    builds its ``Index`` against a throwaway table, so the call's own arguments
    are recorded instead.
    """
    tables: dict = {}
    indexes: list = []

    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": io.StringIO()},
    )
    with Operations.context(context):
        real_create_table = alembic_op.create_table
        real_create_index = alembic_op.create_index

        def create_table(table_name, *columns, **kw):
            table = real_create_table(table_name, *columns, **kw)
            tables[table_name] = table
            return table

        def create_index(index_name, table_name, columns, **kw):
            indexes.append(
                (
                    index_name,
                    table_name,
                    list(columns),
                    bool(kw.get("unique", False)),
                    kw.get("postgresql_where"),
                )
            )
            return real_create_index(index_name, table_name, columns, **kw)

        alembic_op.create_table = create_table
        alembic_op.create_index = create_index
        try:
            migration.upgrade()
        finally:
            alembic_op.create_table = real_create_table
            alembic_op.create_index = real_create_index

    return tables, indexes


@pytest.fixture(scope="module")
def declared():
    return _record_declared_schema(load_assistant_migration("assistant_schema_probe"))


@pytest.fixture(scope="module")
def migration_dump(declared):
    return dump_from_tables(*declared)["tables"]


# --- the migration matches the fixture -------------------------------------


def test_migration_creates_exactly_the_fixtured_tables(declared):
    """A fourth table would otherwise be created but never checked."""
    tables, _ = declared
    assert sorted(tables) == sorted(ASSISTANT_TABLES) == sorted(GOLDEN)


def test_fixture_has_exactly_the_compared_sections():
    for table in GOLDEN.values():
        assert sorted(table) == sorted(SECTIONS)


@pytest.mark.parametrize("table", ASSISTANT_TABLES)
@pytest.mark.parametrize("section", SECTIONS)
def test_migration_matches_golden_fixture(migration_dump, table, section):
    """A failure means production DDL and the fixture disagree. Fix whichever is
    wrong; a fixture change goes into keep-assistant's copy too."""
    assert migration_dump[table][section] == GOLDEN[table][section]


# --- the fixture records what the models can silently lose -----------------


def test_fixture_records_identity_on_seq_only():
    identities = {
        (table, column["name"]): column["identity"]
        for table, entry in GOLDEN.items()
        for column in entry["columns"]
        if column["identity"] is not None
    }
    assert identities == {
        ("assistant_message", "seq"): {"always": False, "start": 1, "increment": 1}
    }


def test_fixture_records_partial_index_predicates():
    predicates = {
        index["name"]: index["where"]
        for entry in GOLDEN.values()
        for index in entry["indexes"]
        if index["where"] is not None
    }
    assert predicates == {
        "ix_assistant_draft_pending_expiry": "state = 'pending'",
        "ix_assistant_message_draft": "draft_id IS NOT NULL",
        "ix_assistant_message_user_rate": "role = 'user'",
    }


def test_fixture_records_check_names_and_sorted_literals():
    checks = {
        check["name"]: check["literals"]
        for entry in GOLDEN.values()
        for check in entry["check_constraints"]
    }
    assert checks == {
        "ck_assistant_draft_state": [
            "applied",
            "applying",
            "discarded",
            "expired",
            "failed",
            "partially_applied",
            "pending",
        ],
        "ck_assistant_message_role": ["assistant", "event", "tool", "user"],
        "ck_assistant_message_status": ["complete", "error", "interrupted"],
    }


def test_fixture_records_ondelete():
    actions = {
        (table, *fk["constrained_columns"]): fk["ondelete"]
        for table, entry in GOLDEN.items()
        for fk in entry["foreign_keys"]
    }
    assert actions == {
        ("assistant_conversation", "tenant_id"): None,
        ("assistant_draft", "tenant_id"): None,
        ("assistant_draft", "conversation_id", "tenant_id"): None,
        ("assistant_message", "tenant_id"): None,
        ("assistant_message", "conversation_id", "tenant_id"): None,
        ("assistant_message", "draft_id"): "SET NULL",
    }


# --- both sources reach the same normal form -------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        (sa.text("state = 'pending'"), "state = 'pending'"),
        # Postgres' spelling for a TEXT and for a VARCHAR column.
        ("(state = 'pending'::text)", "state = 'pending'"),
        ("((state)::text = 'pending'::text)", "state = 'pending'"),
        ("((state)::character varying = 'pending'::text)", "state = 'pending'"),
        ("(draft_id IS NOT NULL)", "draft_id IS NOT NULL"),
        # The spelling of an ORM model's expression.
        (sa.column("state") == "pending", "state = 'pending'"),
        (
            sa.table("assistant_message", sa.column("draft_id")).c.draft_id.is_not(None),
            "draft_id IS NOT NULL",
        ),
        # A literal is data: nothing inside it is normalized away.
        ("(state = 'a.b (c)::text  d'::text)", "state = 'a.b (c)::text  d'"),
    ],
)
def test_predicates_normalize(raw, expected):
    assert _canonical_predicate(raw) == expected


@pytest.mark.parametrize(
    "other",
    ["state = 'applying'", "state <> 'pending'", "kind = 'pending'"],
)
def test_different_predicates_stay_different(other):
    assert _canonical_predicate(other) != _canonical_predicate("state = 'pending'")


@pytest.mark.parametrize(
    "sqltext",
    [
        "role IN ('user', 'assistant')",
        "role = ANY (ARRAY['user'::text, 'assistant'::text])",
        "(role)::text = ANY ((ARRAY['user'::character varying, "
        "'assistant'::character varying])::text[])",
        sa.column("role").in_(["user", "assistant"]),
    ],
)
def test_checks_normalize(sqltext):
    assert _check_entry("ck", sqltext) == {
        "name": "ck",
        "literals": ["assistant", "user"],
    }


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        (sa.text("now()"), "now()"),
        ("now()", "now()"),
        ("pending", "pending"),
        ("'pending'::text", "pending"),
        ("'pending'::character varying", "pending"),
        (sa.text("true"), "true"),
        (sa.true(), "true"),
        ("true", "true"),
    ],
)
def test_server_defaults_normalize(raw, expected):
    assert _canonical_default(raw) == expected


@pytest.mark.parametrize(
    "ondelete,expected",
    [
        (None, None),
        ("NO ACTION", None),
        ("set null", "SET NULL"),
        ("CASCADE", "CASCADE"),
    ],
)
def test_ondelete_normalizes(ondelete, expected):
    entry = _foreign_key_entry(["a"], "t", ["id"], ondelete)
    assert entry["ondelete"] == expected


def test_reflected_postgres_schema_reaches_the_golden_form():
    """Postgres' own spelling of `assistant_message`, models built on VARCHAR."""

    def column(name, type_, nullable=True, default=None, **extra):
        return {
            "name": name,
            "type": type_,
            "nullable": nullable,
            "default": default,
            **extra,
        }

    varchar, uuid = sa.VARCHAR(), postgresql.UUID()
    inspector = SimpleNamespace(
        get_columns=lambda _: [
            column("id", uuid, False),
            column("tenant_id", varchar, False),
            column("conversation_id", uuid, False),
            column(
                "seq",
                sa.BIGINT(),
                False,
                identity={
                    "always": False,
                    "start": 1,
                    "increment": 1,
                    "minvalue": 1,
                    "maxvalue": 9223372036854775807,
                    "cycle": False,
                    "cache": 1,
                },
            ),
            column("role", varchar, False),
            column("status", varchar, False, "'complete'::character varying"),
            column("content", sa.TEXT()),
            column("tool_calls", postgresql.JSONB()),
            column("tool_call_id", varchar),
            column("draft_id", uuid),
            column("model", varchar),
            column("prompt_tokens", sa.INTEGER()),
            column("completion_tokens", sa.INTEGER()),
            column(
                "created_at", postgresql.TIMESTAMP(timezone=True), False, "now()"
            ),
        ],
        get_pk_constraint=lambda _: {"constrained_columns": ["id"]},
        get_indexes=lambda _: [
            {
                "name": "ix_assistant_message_user_rate",
                "column_names": ["conversation_id", "created_at"],
                "unique": False,
                "dialect_options": {"postgresql_where": "((role)::text = 'user'::text)"},
            },
            {
                "name": "ix_assistant_message_draft",
                "column_names": ["draft_id"],
                "unique": False,
                "dialect_options": {"postgresql_where": "(draft_id IS NOT NULL)"},
            },
            {
                "name": "ix_assistant_message_conversation",
                "column_names": ["conversation_id", "seq"],
                "unique": False,
            },
        ],
        get_unique_constraints=lambda _: [],
        get_foreign_keys=lambda _: [
            {
                "constrained_columns": ["draft_id"],
                "referred_table": "assistant_draft",
                "referred_columns": ["id"],
                "options": {"ondelete": "SET NULL"},
            },
            {
                "constrained_columns": ["conversation_id", "tenant_id"],
                "referred_table": "assistant_conversation",
                "referred_columns": ["id", "tenant_id"],
                "options": {},
            },
            {
                "constrained_columns": ["tenant_id"],
                "referred_table": "tenant",
                "referred_columns": ["id"],
                "options": {},
            },
        ],
        get_check_constraints=lambda _: [
            {
                "name": "ck_assistant_message_status",
                "sqltext": "(status)::text = ANY ((ARRAY["
                "'complete'::character varying, 'interrupted'::character varying, "
                "'error'::character varying])::text[])",
            },
            {
                "name": "ck_assistant_message_role",
                "sqltext": "role = ANY (ARRAY['user'::text, 'assistant'::text, "
                "'tool'::text, 'event'::text])",
            },
        ],
    )
    dumped = dump_from_inspector(inspector, ["assistant_message"])["tables"]
    assert dumped == {"assistant_message": GOLDEN["assistant_message"]}
