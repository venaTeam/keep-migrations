"""Execution coverage for the `add_dashboard_image` revision.

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
    "2026-09-24-00-00_add_dashboard_image.py",
)

EXPECTED_COLUMNS = {
    "id": False,
    "tenant_id": False,
    "dashboard_id": True,
    "name": False,
    "content_type": False,
    "size_bytes": False,
    "image_blob": False,
    "created_by": True,
    "created_at": False,
}


def _load():
    spec = importlib.util.spec_from_file_location("add_dashboard_image", MIGRATION_PATH)
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
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'images.db'}")
    md = sa.MetaData()
    sa.Table("tenant", md, sa.Column("id", sa.String(), primary_key=True))
    sa.Table("dashboard", md, sa.Column("id", sa.String(), primary_key=True))
    md.create_all(engine)
    try:
        yield engine
    finally:
        engine.dispose()


def test_revision_chain(migration):
    assert migration.revision == "add_dashboard_image"
    assert migration.down_revision == "derive_alert_suppression"


def test_upgrade_creates_table_columns_and_nullability(engine, migration):
    _run(engine, migration.upgrade)
    columns = {c["name"]: c for c in sa.inspect(engine).get_columns("dashboardimage")}
    assert set(columns) == set(EXPECTED_COLUMNS)
    for name, nullable in EXPECTED_COLUMNS.items():
        assert columns[name]["nullable"] is nullable, name


def test_upgrade_creates_indexes(engine, migration):
    _run(engine, migration.upgrade)
    indexes = {
        i["name"]: i["column_names"]
        for i in sa.inspect(engine).get_indexes("dashboardimage")
    }
    assert indexes["ix_dashboardimage_tenant_id"] == ["tenant_id"]
    assert indexes["ix_dashboardimage_dashboard_id"] == ["dashboard_id"]


def test_upgrade_creates_foreign_keys(engine, migration):
    _run(engine, migration.upgrade)
    fks = {
        fk["constrained_columns"][0]: fk
        for fk in sa.inspect(engine).get_foreign_keys("dashboardimage")
    }
    assert fks["tenant_id"]["referred_table"] == "tenant"
    assert fks["dashboard_id"]["referred_table"] == "dashboard"
    assert fks["dashboard_id"]["options"].get("ondelete") == "CASCADE"


def test_downgrade_drops_table(engine, migration):
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    assert "dashboardimage" not in sa.inspect(engine).get_table_names()


def test_downgrade_emits_drop_table_so_check_refuses_it(migration):
    """keep-migrate --check refuses any path whose SQL contains DROP TABLE."""
    import io

    buffer = io.StringIO()
    ctx = MigrationContext.configure(
        dialect_name="postgresql", opts={"as_sql": True, "output_buffer": buffer}
    )
    with Operations.context(ctx):
        migration.downgrade()
    assert "DROP TABLE dashboardimage" in buffer.getvalue()
