"""add lastalert.maintenance_window_id: the "Hide From Feed" window hiding an alert

The event-handler used to drop alerts matched by a hide-mode maintenance window
before storing them, so they were lost even after the window ended. It now
stores them and records the matching window's id here; the gateway's feed query
hides the row only while that window is still active (exists, enabled, hide
mode, not ended). Nothing rewrites the row when the window ends.

No foreign key on purpose: deleting a window must leave the id behind (a
dangling id reads as "visible") rather than block the delete or rewrite
lastalert. The partial index serves the keep-workflows watcher step that finds
rows whose window is no longer active; the feed query reads the column only
through rows it has already selected.

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


def upgrade() -> None:
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
    op.create_index(
        INDEX,
        TABLE,
        [COLUMN],
        postgresql_where=sa.text(f"{COLUMN} IS NOT NULL"),
        sqlite_where=sa.text(f"{COLUMN} IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    with op.batch_alter_table(TABLE, schema=None) as batch_op:
        batch_op.drop_column(COLUMN)
