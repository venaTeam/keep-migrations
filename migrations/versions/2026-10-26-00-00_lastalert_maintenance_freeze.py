"""add lastalert maintenance freeze columns: hold back updates to an alert on screen

A "Hide From Feed" window hides alerts that are new (or were resolved) when it
matches them. An alert already on screen and unresolved stays visible instead,
and the window now freezes it: its new occurrences are stored in history but
the feed keeps showing the occurrence from before the window. When the window
stops being active, the keep-workflows watcher applies the held-back state so
the feed shows the latest occurrence.

  maintenance_frozen_window_id       the window freezing the row; NULL while
                                     something is held back means "released,
                                     catch up on the next watcher run"
  maintenance_pending_alert_id       latest occurrence held back (alert.id)
  maintenance_pending_last_received  latest receive time held back, including
                                     deduplicated occurrences, which have no alert row
  maintenance_pending_firing_count   occurrences held back, so firing_counter /
                                     unresolved_counter stay exact on catch-up.
                                     NOT NULL (0 for duplicates only) exactly
                                     while the row holds something back

No foreign keys, as for maintenance_window_id: a deleted window leaves a
dangling id, which the watcher reads as "no longer active" and releases.

The columns are added inside the migration transaction: nullable with no
default, so Postgres only updates the catalog. The partial index serves the
watcher's "which rows hold something back" scan, keyed on the pending count
rather than the window id so it also finds rows a window edit released. It
is built with CREATE INDEX CONCURRENTLY on Postgres so the build does not
block alert writes to the live lastalert table, in an autocommit block (same
pattern as lastalert_maintenance_window_id).

No backfill: nothing is frozen before this change. Downgrade drops the
columns, so `keep-migrate --check` refuses it without --allow-destructive; any
row frozen at that moment keeps showing its pre-window occurrence until the
next one arrives.
"""

import sqlalchemy as sa
from alembic import op

revision = "lastalert_maintenance_freeze"
down_revision = "suppression_reason_inactive"
branch_labels = None
depends_on = None

TABLE = "lastalert"
PENDING_FIRING_COUNT = "maintenance_pending_firing_count"
COLUMNS = (
    sa.Column("maintenance_frozen_window_id", sa.Integer(), nullable=True),
    sa.Column("maintenance_pending_alert_id", sa.Uuid(), nullable=True),
    sa.Column(
        "maintenance_pending_last_received", sa.DateTime(timezone=True), nullable=True
    ),
    sa.Column(PENDING_FIRING_COUNT, sa.Integer(), nullable=True),
)
INDEX = "ix_lastalert_maintenance_pending"
INDEX_COLUMNS = ["tenant_id", "fingerprint"]
WHERE = sa.text(f"{PENDING_FIRING_COUNT} IS NOT NULL")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        for column in COLUMNS:
            batch_op.add_column(column.copy())
    if _is_postgres():
        # CONCURRENTLY requires running outside a transaction.
        with op.get_context().autocommit_block():
            op.create_index(
                INDEX,
                TABLE,
                INDEX_COLUMNS,
                postgresql_where=WHERE,
                postgresql_concurrently=True,
                if_not_exists=True,
            )
    else:
        # SQLite test path: plain create (no CONCURRENTLY support).
        op.create_index(
            INDEX, TABLE, INDEX_COLUMNS, sqlite_where=WHERE, if_not_exists=True
        )


def downgrade() -> None:
    if _is_postgres():
        with op.get_context().autocommit_block():
            op.drop_index(
                INDEX,
                table_name=TABLE,
                postgresql_concurrently=True,
                if_exists=True,
            )
    else:
        op.drop_index(INDEX, table_name=TABLE, if_exists=True)
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        for column in reversed(COLUMNS):
            batch_op.drop_column(column.name)
