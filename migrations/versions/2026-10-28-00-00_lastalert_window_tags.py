"""add lastalertmaintenancewindow: every "Hide From Feed" window matching an alert

lastalert.maintenance_window_id (hidden) and maintenance_frozen_window_id
(frozen) hold one window id each. When several active hide-mode windows match
the same occurrence, keeping only one let the alert show again as soon as that
window ended, although another matching window was still running. This table
records all of them, one row per (alert, window, kind):

  kind = 'hide'    the window hides the row (feed excludes it while any is active)
  kind = 'freeze'  the window freezes the row (caught up once none is active)

The two lastalert columns stay as markers: NULL means the row has no tags of
that kind, so untagged rows never read this table. Readers treat the marker's
own window as one more tag, which keeps rows written by an older event-handler
(marker only, no tags) correct during a rolling deploy.

No foreign keys, as for the lastalert columns: a deleted window leaves its
tags behind and they read as inactive, and a tag outlives a deleted lastalert
row harmlessly because writers replace a row's tags whenever they set its
marker. The (tenant_id, maintenance_window_id) index serves window edits and
the keep-workflows watcher.

The table is new and empty, so plain CREATE TABLE / CREATE INDEX take no lock
on lastalert. The backfill copies each marker into one tag, so rows hidden or
frozen at deploy time keep their state; it reads lastalert once and only
touches rows with a marker.
"""

import sqlalchemy as sa
from alembic import op

revision = "lastalert_window_tags"
down_revision = "lastalert_maintenance_freeze"
branch_labels = None
depends_on = None

TABLE = "lastalertmaintenancewindow"
INDEX = "ix_lastalertmaintenancewindow_tenant_window"

lastalert = sa.table(
    "lastalert",
    sa.column("tenant_id", sa.String),
    sa.column("fingerprint", sa.String),
    sa.column("maintenance_window_id", sa.Integer),
    sa.column("maintenance_frozen_window_id", sa.Integer),
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("maintenance_window_id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint(
            "tenant_id", "fingerprint", "kind", "maintenance_window_id"
        ),
    )
    op.create_index(INDEX, TABLE, ["tenant_id", "maintenance_window_id"])

    tags = sa.table(
        TABLE,
        sa.column("tenant_id", sa.String),
        sa.column("fingerprint", sa.String),
        sa.column("kind", sa.String),
        sa.column("maintenance_window_id", sa.Integer),
    )
    for kind, marker in (
        ("hide", lastalert.c.maintenance_window_id),
        ("freeze", lastalert.c.maintenance_frozen_window_id),
    ):
        op.execute(
            tags.insert().from_select(
                ["tenant_id", "fingerprint", "kind", "maintenance_window_id"],
                sa.select(
                    lastalert.c.tenant_id,
                    lastalert.c.fingerprint,
                    sa.literal(kind),
                    marker,
                ).where(marker.is_not(None)),
            )
        )


def downgrade() -> None:
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
