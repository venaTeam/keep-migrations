"""Execution coverage for the `lastalert_maintenance_window_id` revision.

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
    "2026-10-20-00-00_lastalert_maintenance_window_id.py",
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "lastalert_maintenance_window_id", MIGRATION_PATH
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
    assert migration.revision == "lastalert_maintenance_window_id"
    assert migration.down_revision == "add_dashboard_image"


def test_upgrade_adds_nullable_column_and_index(engine, migration):
    _run(engine, migration.upgrade)
    columns = _columns(engine)
    assert columns["maintenance_window_id"]["nullable"] is True
    assert "ix_lastalert_maintenance_window_id" in _indexes(engine)


def test_upgrade_keeps_existing_rows_visible(engine, migration):
    _run(engine, migration.upgrade)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text("SELECT status, maintenance_window_id FROM lastalert")
        ).one()
    assert row.status == "firing"
    assert row.maintenance_window_id is None


def test_downgrade_removes_column_and_index(engine, migration):
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    assert "maintenance_window_id" not in _columns(engine)
    assert "ix_lastalert_maintenance_window_id" not in _indexes(engine)
