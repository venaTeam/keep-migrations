"""Execution coverage for the `lastalert_maintenance_freeze` revision.

Loads the revision by path and drives its real upgrade()/downgrade() through
alembic's MigrationContext against a SQLite database we own, as the other
tests/test_*_migration.py files do.
"""

import importlib.util
import os

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

MIGRATION_PATH = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "migrations",
    "versions",
    "2026-10-26-00-00_lastalert_maintenance_freeze.py",
)

NEW_COLUMNS = (
    "maintenance_frozen_window_id",
    "maintenance_pending_alert_id",
    "maintenance_pending_last_received",
    "maintenance_pending_firing_count",
)
INDEX = "ix_lastalert_maintenance_pending"


def _load():
    spec = importlib.util.spec_from_file_location(
        "lastalert_maintenance_freeze", MIGRATION_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run(engine, step):
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            step()


@pytest.fixture
def migration():
    return _load()


@pytest.fixture
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'lastalert.db'}")
    md = sa.MetaData()
    sa.Table(
        "lastalert",
        md,
        sa.Column("tenant_id", sa.String(), primary_key=True),
        sa.Column("fingerprint", sa.String(), primary_key=True),
        sa.Column("status", sa.String(), nullable=True),
        sa.Column("maintenance_window_id", sa.Integer(), nullable=True),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO lastalert (tenant_id, fingerprint, status) "
                "VALUES ('keep', 'fp-1', 'firing')"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _columns(engine):
    return {c["name"]: c for c in sa.inspect(engine).get_columns("lastalert")}


def _indexes(engine):
    return {i["name"] for i in sa.inspect(engine).get_indexes("lastalert")}


def test_revision_chain(migration):
    assert migration.revision == "lastalert_maintenance_freeze"
    assert migration.down_revision == "suppression_reason_inactive"


def test_is_the_single_head():
    from alembic.script import ScriptDirectory

    scripts = ScriptDirectory(
        os.path.join(os.path.dirname(os.path.dirname(__file__)), "migrations")
    )
    assert scripts.get_heads() == ["lastalert_maintenance_freeze"]


def test_upgrade_adds_nullable_columns_and_index(engine, migration):
    _run(engine, migration.upgrade)
    columns = _columns(engine)
    for name in NEW_COLUMNS:
        assert columns[name]["nullable"] is True, name
    assert INDEX in _indexes(engine)


def test_index_is_partial_on_holding_rows(engine, migration):
    _run(engine, migration.upgrade)
    with engine.connect() as conn:
        ddl = conn.execute(
            sa.text("SELECT sql FROM sqlite_master WHERE name = :name"),
            {"name": INDEX},
        ).scalar_one()
    assert "WHERE maintenance_pending_firing_count IS NOT NULL" in ddl


def test_upgrade_leaves_existing_rows_unfrozen(engine, migration):
    _run(engine, migration.upgrade)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(
                "SELECT status, " + ", ".join(NEW_COLUMNS) + " FROM lastalert"
            )
        ).one()
    assert row.status == "firing"
    for name in NEW_COLUMNS:
        assert getattr(row, name) is None, name


def test_downgrade_removes_columns_and_index(engine, migration):
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    columns = _columns(engine)
    for name in NEW_COLUMNS:
        assert name not in columns, name
    assert "maintenance_window_id" in columns
    assert INDEX not in _indexes(engine)
