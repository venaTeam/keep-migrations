"""add dashboardimage: images uploaded for dashboard IMAGE widgets

A row is created pending (dashboard_id NULL) by keep-api-gateway's
POST /dashboard-images and is claimed by the dashboard save that references it,
in the same transaction. Deleting a dashboard deletes its images; the FK's
ON DELETE CASCADE is only a backstop for that.

No backfill: the table is new and no existing dashboard references an image.
Downgrade drops the table, so `keep-migrate --check` refuses it without
--allow-destructive.
"""

import sqlalchemy as sa
import sqlmodel
from alembic import op
from sqlalchemy.dialects import mysql

revision = "add_dashboard_image"
down_revision = "derive_alert_suppression"
branch_labels = None
depends_on = None

TABLE = "dashboardimage"


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("tenant_id", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("dashboard_id", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("content_type", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column(
            "image_blob",
            sa.LargeBinary().with_variant(mysql.LONGBLOB(), "mysql"),
            nullable=False,
        ),
        sa.Column("created_by", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenant.id"]),
        sa.ForeignKeyConstraint(["dashboard_id"], ["dashboard.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dashboardimage_tenant_id", TABLE, ["tenant_id"])
    op.create_index("ix_dashboardimage_dashboard_id", TABLE, ["dashboard_id"])


def downgrade() -> None:
    op.drop_index("ix_dashboardimage_dashboard_id", table_name=TABLE)
    op.drop_index("ix_dashboardimage_tenant_id", table_name=TABLE)
    op.drop_table(TABLE)
