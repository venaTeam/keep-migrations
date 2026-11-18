"""incident: add dismiss columns, drop the 'deleted' status, make delete a real delete

Three coupled changes behind the new incident suppression behaviour:

1. `incident.dismiss_mode` / `incident.dismissed_until` — the typed dismiss
   state, mirroring what `lastalert` already carries for alerts. "suppressed"
   is NOT written to `incident.status`; it is derived from these two columns at
   read time (`Incident.get_effective_status`) so a time-boxed dismissal can
   expire on its own clock without a sweeper job rewriting rows.

2. `IncidentStatus.DELETED` is gone, so rows still parked in `status='deleted'`
   are unreachable — nothing can query them by status and nothing will ever
   move them out of it. They are deleted for real here.

3. `incidentenrichment_incident_id_fkey` was NO ACTION, which made a hard
   DELETE of any enriched incident fail with a foreign key violation. Recreated
   ON DELETE CASCADE, matching the CASCADE that `lastalerttoincident` and
   `alerttoincident` already declare. Without this, both the backfill in (2)
   and `delete_incident_by_id` raise on exactly the incidents users care about.

IRREVERSIBLE IN PART: `downgrade()` drops the two columns and restores the
FK's NO ACTION rule, but cannot bring back the rows deleted in (2). Take a
backup if those rows still matter.

(2) runs as a batched delete (2000 per statement) after adding the
`idx_lastalerttoincident_incident_id` index, so the cascade scans are
indexed and neither step holds locks long enough to block traffic —
observed unbatched/unindexed as ~8+ minutes of full-table scans holding
locks on stage.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "incident_dismiss_columns"
down_revision = "lastalert_window_tags"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "incident",
        sa.Column("dismiss_mode", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "incident",
        sa.Column("dismissed_until", sa.DateTime(timezone=True), nullable=True),
    )
    # Partial-ish index: the read-time derivation filters on dismissed_until for
    # every incident list query, and the column is NULL for all but dismissed rows.
    op.create_index(
        "ix_incident_dismissed_until",
        "incident",
        ["dismissed_until"],
    )

    # Enrichment rows must follow their incident to the grave now that delete is
    # a real DELETE. Drop-and-recreate is the only way to change a FK's action.
    op.drop_constraint(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        "incident",
        ["incident_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # Soft-deleted incidents are unreachable now that the status is gone.
    # lastalerttoincident / alerttoincident / incidentenrichment all cascade;
    # self-referential merge and same-incident links are ON DELETE SET NULL.
    #
    # The cascades made this DELETE pathological on stage: lastalerttoincident's
    # PK is (tenant_id, incident_id, fingerprint, deleted_at), so incident_id is
    # not its leading column and every per-incident cascade scan walked the
    # whole table (~78k rows x ~144k deleted incidents, minutes of lock-hold,
    # every stage query queued behind it). Index the leading-edge first, then
    # delete in batches so each chunk's locks are held for milliseconds and
    # traffic interleaves instead of queueing behind one huge transaction.
    op.create_index(
        "idx_lastalerttoincident_incident_id",
        "lastalerttoincident",
        ["incident_id"],
    )
    # Self-referential FKs on incident (merged_into_incident_id,
    # same_incident_in_the_past_id) have no leading index, so every cascade
    # FK check per deleted row seq-scans the full incident table. Created
    # before the delete so the purged rows' cascade checks are index lookups.
    op.create_index(
        "idx_incident_merged_into",
        "incident",
        ["merged_into_incident_id"],
        postgresql_where=sa.text("merged_into_incident_id IS NOT NULL"),
    )
    op.create_index(
        "idx_incident_same_past",
        "incident",
        ["same_incident_in_the_past_id"],
        postgresql_where=sa.text("same_incident_in_the_past_id IS NOT NULL"),
    )

    batch_size = 2000
    conn = op.get_bind()
    while True:
        result = conn.execute(
            sa.text(
                "DELETE FROM incident WHERE id IN ("
                "SELECT id FROM incident WHERE status = 'deleted' LIMIT :batch)"
            ).bindparams(batch=batch_size)
        )
        # --sql / --check render without a database: execute() returns None.
        if result is None or result.rowcount < batch_size:
            break


def downgrade() -> None:
    op.drop_constraint(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "incidentenrichment_incident_id_fkey",
        "incidentenrichment",
        "incident",
        ["incident_id"],
        ["id"],
    )
    op.drop_index("ix_incident_dismissed_until", table_name="incident")
    op.drop_column("incident", "dismissed_until")
    op.drop_column("incident", "dismiss_mode")
    op.drop_index("idx_incident_merged_into", table_name="incident")
    op.drop_index("idx_incident_same_past", table_name="incident")
    op.drop_index("idx_lastalerttoincident_incident_id", table_name="lastalerttoincident")