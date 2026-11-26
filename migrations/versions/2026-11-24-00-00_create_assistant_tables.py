"""create the Keep Assistant tables: conversation, draft and message

The gateway's assistant package is the only reader and writer, and its ORM models
must match these tables. They are deliberately not registered in the gateway's
`all_models.py`: without them the gateway stays ready and only its assistant
routes answer 503.

TENANCY. Every table carries `tenant_id` NOT NULL with an FK to `tenant.id`. The
composite FK `(conversation_id, tenant_id)` also makes a child row whose tenant
differs from its conversation's unwritable. `assistant_draft` and
`assistant_message` have no index leading with `tenant_id`, which Postgres does
not add for FK columns: no code deletes tenants, so nothing needs the reverse
lookup, and reads reach these rows through the conversation.

NO CASCADES, except `assistant_message.draft_id` ON DELETE SET NULL, so a
retention run that deletes drafts never trips over a message still pointing at
one. Every FK column that retention deletes through leads an index.

LOCKS. The tables are new and empty, but the FKs to `tenant` take SHARE ROW
EXCLUSIVE on `tenant` until the release transaction ends: tenant writes wait,
reads do not. The downgrade takes ACCESS EXCLUSIVE on `tenant`, which blocks
reads too, so it bounds its wait first and fails rather than queueing every
service behind it.

Revision ID: create_assistant_tables
Revises: lastalert_window_tags
Create Date: 2026-11-24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "create_assistant_tables"
down_revision = "lastalert_window_tags"
branch_labels = None
depends_on = None


def _jsonb():
    """JSONB on Postgres, JSON elsewhere (SQLite test engine)."""
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _timestamp(name, *, nullable):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def _timestamp_now(name):
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def upgrade() -> None:
    op.create_table(
        "assistant_conversation",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        # The gateway identity's email, as whoami returns it. The owner of a
        # conversation is (tenant_id, created_by).
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        # Turn lock: set while a chat turn runs, stale after a timeout.
        sa.Column("active_turn_id", sa.Uuid(), nullable=True),
        _timestamp("turn_started_at", nullable=True),
        _timestamp_now("created_at"),
        _timestamp_now("updated_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_assistant_conversation_tenant"
        ),
        # Redundant with the PK as a uniqueness rule; it exists as the target of
        # the children's composite FK.
        sa.UniqueConstraint(
            "id", "tenant_id", name="uq_assistant_conversation_id_tenant_id"
        ),
    )
    # A user's own conversations, by recency.
    op.create_index(
        "ix_assistant_conversation_owner",
        "assistant_conversation",
        ["tenant_id", "created_by", "updated_at"],
    )
    # Retention, deliberately cross-tenant.
    op.create_index(
        "ix_assistant_conversation_updated_at",
        "assistant_conversation",
        ["updated_at"],
    )

    op.create_table(
        "assistant_draft",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        # The tool that produced the draft. An open set owned by the assistant's
        # tool registry, so there is no CHECK.
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False, server_default="pending"),
        sa.Column(
            "permitted", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("payload", _jsonb(), nullable=False),
        sa.Column("outcome", _jsonb(), nullable=True),
        # Always written as now() + ttl, so no default.
        _timestamp("expires_at", nullable=False),
        _timestamp("resolved_at", nullable=True),
        _timestamp_now("created_at"),
        _timestamp_now("updated_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_assistant_draft_tenant"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["assistant_conversation.id", "assistant_conversation.tenant_id"],
            name="fk_assistant_draft_conversation_tenant",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'applying', 'applied', 'partially_applied', "
            "'failed', 'discarded', 'expired')",
            name="ck_assistant_draft_state",
        ),
    )
    # Leads with the FK column, so it also serves the FK check when a
    # conversation is deleted.
    op.create_index(
        "ix_assistant_draft_conversation",
        "assistant_draft",
        ["conversation_id", "created_at"],
    )
    # The expiry sweep, deliberately cross-tenant.
    op.create_index(
        "ix_assistant_draft_pending_expiry",
        "assistant_draft",
        ["expires_at"],
        postgresql_where=sa.text("state = 'pending'"),
    )

    op.create_table(
        "assistant_message",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("tenant_id", sa.String(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        # Insert order. now() is constant inside a transaction, so created_at
        # cannot order a tool-call group written in one.
        sa.Column("seq", sa.BigInteger(), sa.Identity(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default="complete"),
        sa.Column("content", sa.Text(), nullable=True),
        sa.Column("tool_calls", _jsonb(), nullable=True),
        sa.Column("tool_call_id", sa.Text(), nullable=True),
        sa.Column("draft_id", sa.Uuid(), nullable=True),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        _timestamp_now("created_at"),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name="fk_assistant_message_tenant"
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id", "tenant_id"],
            ["assistant_conversation.id", "assistant_conversation.tenant_id"],
            name="fk_assistant_message_conversation_tenant",
        ),
        sa.ForeignKeyConstraint(
            ["draft_id"],
            ["assistant_draft.id"],
            name="fk_assistant_message_draft",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "role IN ('user', 'assistant', 'tool', 'event')",
            name="ck_assistant_message_role",
        ),
        sa.CheckConstraint(
            "status IN ('complete', 'interrupted', 'error')",
            name="ck_assistant_message_status",
        ),
    )
    # History and timeline reads; also serves the conversation FK check.
    op.create_index(
        "ix_assistant_message_conversation",
        "assistant_message",
        ["conversation_id", "seq"],
    )
    # The ON DELETE SET NULL lookup. Partial: few messages carry a draft.
    op.create_index(
        "ix_assistant_message_draft",
        "assistant_message",
        ["draft_id"],
        postgresql_where=sa.text("draft_id IS NOT NULL"),
    )
    # The per-user rate limit counts user messages in a time window.
    op.create_index(
        "ix_assistant_message_user_rate",
        "assistant_message",
        ["conversation_id", "created_at"],
        postgresql_where=sa.text("role = 'user'"),
    )


def downgrade() -> None:
    # See LOCKS above. Transaction-scoped, and Postgres-only syntax.
    if op.get_context().dialect.name == "postgresql":
        op.execute("SET LOCAL lock_timeout = '5s'")
    # Children first; each table's indexes go with it.
    op.drop_table("assistant_message")
    op.drop_table("assistant_draft")
    op.drop_table("assistant_conversation")
