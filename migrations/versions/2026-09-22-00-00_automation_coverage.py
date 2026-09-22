"""Add fingerprint-level automation coverage (B6).

Revision ID: automation_coverage
Revises: merge_automations_preset_tag
"""

from alembic import op
import sqlalchemy as sa

revision = "automation_coverage"
down_revision = "merge_automations_preset_tag"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "lastalert",
        sa.Column("automation_matched", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("lastalert", sa.Column("grace_seconds", sa.Integer(), nullable=True))


def downgrade():
    op.drop_column("lastalert", "grace_seconds")
    op.drop_column("lastalert", "automation_matched")
