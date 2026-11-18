"""Execution coverage for the `suppressed_status_to_dismissal` revision.

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
    "2026-11-18-00-00_suppressed_status_to_dismissal.py",
)


def _load():
    spec = importlib.util.spec_from_file_location(
        "suppressed_status_to_dismissal", MIGRATION_PATH
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
        sa.Column("dismiss_mode", sa.String(), nullable=True),
        sa.Column("dismissed_until", sa.DateTime(timezone=True), nullable=True),
    )
    md.create_all(engine)
    return engine


def _insert(engine, rows):
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO lastalert (tenant_id, fingerprint, status, dismiss_mode,"
                " dismissed_until) VALUES (:tenant_id, :fingerprint, :status,"
                " :dismiss_mode, :dismissed_until)"
            ),
            rows,
        )


def _rows(engine):
    with engine.connect() as conn:
        return {
            row.fingerprint: (row.status, row.dismiss_mode, row.dismissed_until)
            for row in conn.execute(
                sa.text(
                    "SELECT fingerprint, status, dismiss_mode, dismissed_until"
                    " FROM lastalert"
                )
            )
        }


def _row(fingerprint, status=None, dismiss_mode=None, dismissed_until=None):
    return {
        "tenant_id": "keep",
        "fingerprint": fingerprint,
        "status": status,
        "dismiss_mode": dismiss_mode,
        "dismissed_until": dismissed_until,
    }


def test_revision_chains_after_derive_alert_suppression(migration):
    assert migration.revision == "suppressed_status_to_dismissal"
    assert migration.down_revision == "derive_alert_suppression"


def test_status_suppressed_becomes_a_permanent_dismissal(engine, migration):
    _insert(engine, [_row("dropdown", status="suppressed")])

    _run(engine, migration.upgrade)

    assert _rows(engine)["dropdown"] == (None, "permanent", None)


def test_other_rows_are_left_alone(engine, migration):
    _insert(
        engine,
        [
            _row("acked", status="acknowledged"),
            _row("plain"),
            _row("modal-permanent", dismiss_mode="permanent"),
            _row(
                "modal-until",
                status="acknowledged",
                dismiss_mode="dismiss_until",
                dismissed_until="2030-01-01 00:00:00",
            ),
            # A dismissal already carries it; derive_alert_suppression's case.
            _row("both", status="suppressed", dismiss_mode="dismiss_until"),
        ],
    )
    before = _rows(engine)

    _run(engine, migration.upgrade)

    assert _rows(engine) == before


def test_converts_every_row_across_batches(engine, migration, monkeypatch):
    monkeypatch.setattr(migration, "_BATCH_SIZE", 3)
    _insert(
        engine,
        [_row(f"fp-{i}", status="suppressed") for i in range(7)]
        + [_row("acked", status="acknowledged")],
    )

    _run(engine, migration.upgrade)

    rows = _rows(engine)
    assert all(rows[f"fp-{i}"] == (None, "permanent", None) for i in range(7))
    assert rows["acked"] == ("acknowledged", None, None)


def test_downgrade_is_a_no_op(engine, migration):
    _insert(engine, [_row("dropdown", status="suppressed")])
    _run(engine, migration.upgrade)
    after_upgrade = _rows(engine)

    _run(engine, migration.downgrade)

    assert _rows(engine) == after_upgrade
