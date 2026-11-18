"""store a dropdown-suppressed alert as a permanent dismissal, not as its status

An alert's suppression is derived from `lastalert.dismiss_mode` /
`dismissed_until` on read, and `lastalert.status` holds the user's override
that the alert reverts to when a dismissal ends. The alert status dropdown
still wrote `status='suppressed'` with no dismiss mode: a suppression that
never lapses, and a nonsensical status to "revert" to. Every path that hands an
alert a dismissal (linking it to a dismissed incident, the dismissal-expiry
sweep) had to special-case that value.

The write path now saves a requested `status='suppressed'` as
`dismiss_mode='permanent'`. This converts the rows written before it, so no
`lastalert.status` holds 'suppressed' any more. `derive_alert_suppression`
already cleared the rows that also carried a dismiss mode; this takes the rest.

Untouched: `alert.status`, the alert's own provider status, which maintenance
windows can still set to 'suppressed'.

Batched (2000 rows per statement) like `incident_dismiss_columns`, so each
statement holds its row locks briefly. A converted row stops matching, so the
loop ends once none are left.

`downgrade()` is a no-op: the converted rows are valid at the previous revision
too (it reads a permanent dismissal as suppressed), and they can't be told
apart from alerts dismissed permanently through the dismiss modal.
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "suppressed_status_to_dismissal"
down_revision = "derive_alert_suppression"
branch_labels = None
depends_on = None

_BATCH_SIZE = 2000

_CONVERT_SQL = sa.text(
    """
    UPDATE lastalert
    SET dismiss_mode = 'permanent', dismissed_until = NULL, status = NULL
    WHERE (tenant_id, fingerprint) IN (
        SELECT tenant_id, fingerprint FROM lastalert
        WHERE status = 'suppressed' AND dismiss_mode IS NULL
        LIMIT :batch
    )
    """
)


def upgrade() -> None:
    conn = op.get_bind()
    converted = 0
    while True:
        result = conn.execute(_CONVERT_SQL.bindparams(batch=_BATCH_SIZE))
        # --sql / --check render without a database: execute() returns None.
        if result is None:
            return
        converted += result.rowcount
        if result.rowcount < _BATCH_SIZE:
            break
    print(f"converted {converted} status-suppressed alerts to permanent dismissals")


def downgrade() -> None:
    pass
