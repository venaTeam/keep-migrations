"""add `build_succeeded` / `build_failed` to automation_revision_action

Revision ID: revision_action_build_outcomes
Revises: suppression_reason_inactive
Create Date: 2026-10-27 00:00:00.000000

The CI webhook (keep-automation-api D15) moves an automation out of `building`:
success cuts it over to a new image, failure marks it `build_failed`. Spec §8.4
requires every state transition to be audited, but `automation_revisions` only
had the human actions (`create | edit | enable | disable | delete`), so a build
outcome left no row — a `build_failed` was overwritten by the next edit without
a trace. Each applied webhook now writes one revision row with the outcome, the
commit SHA, and (on success) the digest now serving (automation-contracts.md
§"DB enums", §"CI webhook").

Postgres cannot add an enum value and use it in the same transaction, and older
servers refuse `ALTER TYPE ... ADD VALUE` inside a transaction block at all, so
the statements run in an autocommit block. `IF NOT EXISTS` makes a re-run after
a partial deploy a no-op.

Downgrade: Postgres has no `DROP VALUE`, so the type is rebuilt without them.
`action` is NOT NULL, so revision rows that used the new values are deleted —
the only way back to the five-value type, and therefore a destructive
downgrade (build history is lost; the human create/edit rows keep their
`resulting_digest`).

On non-Postgres dialects the column is VARCHAR + CHECK and the hermetic tests
never write the new values; nothing is emitted there.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "revision_action_build_outcomes"
down_revision = "suppression_reason_inactive"
branch_labels = None
depends_on = None

ENUM_TYPE = "automation_revision_action"
ADDED_VALUES = ("build_succeeded", "build_failed")
PREVIOUS_VALUES = ("create", "edit", "enable", "disable", "delete")


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        for value in ADDED_VALUES:
            op.execute(f"ALTER TYPE {ENUM_TYPE} ADD VALUE IF NOT EXISTS '{value}'")


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    added = ", ".join(f"'{value}'" for value in ADDED_VALUES)
    previous = ", ".join(f"'{value}'" for value in PREVIOUS_VALUES)
    op.execute(f"DELETE FROM automation_revisions WHERE action IN ({added})")
    op.execute(f"ALTER TYPE {ENUM_TYPE} RENAME TO {ENUM_TYPE}_old")
    op.execute(f"CREATE TYPE {ENUM_TYPE} AS ENUM ({previous})")
    op.execute(
        f"ALTER TABLE automation_revisions ALTER COLUMN action "
        f"TYPE {ENUM_TYPE} USING action::text::{ENUM_TYPE}"
    )
    op.execute(f"DROP TYPE {ENUM_TYPE}_old")
