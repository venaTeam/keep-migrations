"""add lastalert.maintenance_window_id: the "Hide From Feed" window hiding an alert

The event-handler used to drop alerts matched by a hide-mode maintenance window
before storing them, so they were lost even after the window ended. It now
stores them and records the matching window's id here; the gateway's feed query
hides the row only while that window is still active (exists, enabled, hide
mode, not ended). Nothing rewrites the row when the window ends.

No foreign key on purpose: deleting a window must leave the id behind (a
dangling id reads as "visible") rather than block the delete or rewrite
lastalert. The partial index serves the keep-workflows watcher step that checks
whether a window that just ended still tags any alerts; the feed query reads
the column only through rows it has already selected.

The column is added inside the migration transaction: nullable with no
default, so Postgres only updates the catalog. The index is built with CREATE
INDEX CONCURRENTLY on Postgres, so the build does not block alert writes to the
live lastalert table; CONCURRENTLY cannot run inside a transaction, so it runs
in an autocommit block (same pattern as preset_filter_indexes).

No backfill: the column is new and alerts dropped before this change are gone.
Downgrade drops the column, so `keep-migrate --check` refuses it without
--allow-destructive.
"""

import sqlalchemy as sa
from alembic import op

revision = "lastalert_maintenance_window_id"
down_revision = "add_dashboard_image"
branch_labels = None
depends_on = None

TABLE = "lastalert"
COLUMN = "maintenance_window_id"
INDEX = "ix_lastalert_maintenance_window_id"
WHERE = sa.text(f"{COLUMN} IS NOT NULL")


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
    if _is_postgres():
        # CONCURRENTLY requires running outside a transaction.
        with op.get_context().autocommit_block():
            op.create_index(
                INDEX,
                TABLE,
                [COLUMN],
                postgresql_where=WHERE,
                postgresql_concurrently=True,
                if_not_exists=True,
            )
    else:
        # SQLite test path: plain create (no CONCURRENTLY support).
        op.create_index(INDEX, TABLE, [COLUMN], sqlite_where=WHERE, if_not_exists=True)


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
        batch_op.drop_column(COLUMN)
