"""add `inactive` to automation_suppression_reason

Revision ID: automation_suppression_reason_inactive
Revises: merge_automations_preset_tag
Create Date: 2026-09-15 00:00:00.000000

A run row the automation API decides not to invoke because its automation is no
longer `active` — deleted, being deleted, or disabled while the matched message
waited in the topic — is recorded `state='suppressed'`. Until now the reason
enum only had `duplicate | cooldown`, so that case had to be written with a NULL
reason while the submit contract already promised "not active" as a reason
(automation-contracts.md §Submit, §DB enums). D18's
`finalize_terminated_by_deletion` and D17's submit re-check both write it.

Postgres cannot add an enum value and use it in the same transaction, and older
servers refuse `ALTER TYPE ... ADD VALUE` inside a transaction block at all, so
the statement runs in an autocommit block. `IF NOT EXISTS` makes a re-run after a
partial deploy a no-op.

Downgrade: Postgres has no `DROP VALUE`, so the type is rebuilt without it. Rows
that used `inactive` lose their reason (set to NULL) — the only way back to the
two-value type, and therefore a destructive downgrade.

On non-Postgres dialects the column is VARCHAR + CHECK and the hermetic tests
never write `inactive`; nothing is emitted there.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "automation_suppression_reason_inactive"
down_revision = "merge_automations_preset_tag"
branch_labels = None
depends_on = None

ENUM_TYPE = "automation_suppression_reason"
ADDED_VALUE = "inactive"
PREVIOUS_VALUES = ("duplicate", "cooldown")


def upgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    with op.get_context().autocommit_block():
        op.execute(f"ALTER TYPE {ENUM_TYPE} ADD VALUE IF NOT EXISTS '{ADDED_VALUE}'")


def downgrade() -> None:
    if op.get_context().dialect.name != "postgresql":
        return
    previous = ", ".join(f"'{value}'" for value in PREVIOUS_VALUES)
    op.execute(
        f"UPDATE automation_runs SET suppression_reason = NULL "
        f"WHERE suppression_reason = '{ADDED_VALUE}'"
    )
    op.execute(f"ALTER TYPE {ENUM_TYPE} RENAME TO {ENUM_TYPE}_old")
    op.execute(f"CREATE TYPE {ENUM_TYPE} AS ENUM ({previous})")
    op.execute(
        f"ALTER TABLE automation_runs ALTER COLUMN suppression_reason "
        f"TYPE {ENUM_TYPE} USING suppression_reason::text::{ENUM_TYPE}"
    )
    op.execute(f"DROP TYPE {ENUM_TYPE}_old")
