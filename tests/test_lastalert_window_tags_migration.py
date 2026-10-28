"""Execution coverage for the `lastalert_window_tags` revision.

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
    "2026-10-28-00-00_lastalert_window_tags.py",
)
TABLE = "lastalertmaintenancewindow"


def _load():
    spec = importlib.util.spec_from_file_location(
        "lastalert_window_tags", MIGRATION_PATH
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
        sa.Column("maintenance_window_id", sa.Integer(), nullable=True),
        sa.Column("maintenance_frozen_window_id", sa.Integer(), nullable=True),
    )
    md.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO lastalert VALUES "
                "('keep', 'plain', NULL, NULL), "
                "('keep', 'hidden', 7, NULL), "
                "('keep', 'frozen', NULL, 8), "
                "('other', 'hidden', 9, NULL)"
            )
        )
    try:
        yield engine
    finally:
        engine.dispose()


def _tags(engine):
    with engine.connect() as conn:
        return sorted(
            tuple(row)
            for row in conn.execute(
                sa.text(
                    f"SELECT tenant_id, fingerprint, kind, maintenance_window_id FROM {TABLE}"
                )
            )
        )


def test_revision_chain(migration):
    assert migration.revision == "lastalert_window_tags"
    assert migration.down_revision == "lastalert_maintenance_freeze"


def test_revision_id_fits_alembic_version(migration):
    # alembic_version.version_num is VARCHAR(32) on Postgres; SQLite does not
    # enforce it, so a longer id only fails when stamped on a real database.
    assert len(migration.revision) <= 32


def test_upgrade_creates_table_and_index(engine, migration):
    _run(engine, migration.upgrade)
    inspector = sa.inspect(engine)
    assert inspector.get_pk_constraint(TABLE)["constrained_columns"] == [
        "tenant_id",
        "fingerprint",
        "kind",
        "maintenance_window_id",
    ]
    assert "ix_lastalertmaintenancewindow_tenant_window" in {
        i["name"] for i in inspector.get_indexes(TABLE)
    }


def test_upgrade_backfills_one_tag_per_marker(engine, migration):
    _run(engine, migration.upgrade)
    assert _tags(engine) == [
        ("keep", "frozen", "freeze", 8),
        ("keep", "hidden", "hide", 7),
        ("other", "hidden", "hide", 9),
    ]


def test_downgrade_drops_table(engine, migration):
    _run(engine, migration.upgrade)
    _run(engine, migration.downgrade)
    assert TABLE not in sa.inspect(engine).get_table_names()
