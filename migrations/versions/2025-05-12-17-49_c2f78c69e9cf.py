"""Recalculate alerts_count for incidents

Revision ID: c2f78c69e9cf
Revises: 7b687c555318
Create Date: 2025-05-12 17:49:09.779088

"""

from collections import defaultdict
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from sqlalchemy.sql.functions import count

# revision identifiers, used by Alembic.
revision = "c2f78c69e9cf"
down_revision = "7b687c555318"
branch_labels = None
depends_on = None

# Core tables declared locally rather than imported from the ORM layer. A
# migration is a statement about the schema as it stood when the migration was
# written; importing a live model means a later column rename or default change
# silently rewrites what this revision does to a fresh database.
_metadata = sa.MetaData()

_lastalerttoincident = sa.Table(
    "lastalerttoincident",
    _metadata,
    sa.Column("fingerprint", sa.String, primary_key=True),
    sa.Column("incident_id", sa.CHAR(32)),
    sa.Column("deleted_at", sa.DateTime),
)

_incident = sa.Table(
    "incident",
    _metadata,
    sa.Column("id", sa.CHAR(32), primary_key=True),
    sa.Column("alerts_count", sa.Integer),
)

# Frozen copy of helpers.NULL_FOR_DELETED_AT.
NULL_FOR_DELETED_AT = datetime(1000, 1, 1, 0, 0)


def upgrade() -> None:
    session = Session(op.get_bind())
    counts = session.execute(
        select(
            count(_lastalerttoincident.c.fingerprint), _lastalerttoincident.c.incident_id
        )
        .where(_lastalerttoincident.c.deleted_at == NULL_FOR_DELETED_AT)
        .group_by(_lastalerttoincident.c.incident_id)
    ).all()
    counts_per_incident = defaultdict(int)
    for count_, incident_id in counts:
        counts_per_incident[incident_id] = count_

    incident_ids = session.execute(select(_incident.c.id)).scalars().all()

    for incident_id in incident_ids:
        session.execute(
            update(_incident)
            .where(_incident.c.id == incident_id)
            .values(alerts_count=counts_per_incident.get(incident_id, 0))
        )
        session.commit()

def downgrade() -> None:
    pass

