"""Extend dismissed enrichments with SUPPRESSED status

Revision ID: 876a424d8f06
Revises: 8176d7153747
Create Date: 2025-02-18 18:09:40.656808

"""

import json

from alembic import op
from sqlalchemy import Column, MetaData, String, Table, and_, func, null, type_coerce, update
from sqlalchemy.dialects.postgresql import JSONB as PG_JSONB
from sqlmodel import JSON, Session

# Frozen copy of AlertStatus.SUPPRESSED.value. See the note below: this file
# already refuses to depend on the live model layer, and an enum is no different
# from a table -- rename the member and this revision starts writing a different
# value to databases built after the rename.
_SUPPRESSED = "suppressed"

# revision identifiers, used by Alembic.
revision = "876a424d8f06"
down_revision = "8176d7153747"
branch_labels = None
depends_on = None


# Self-contained Core table reflection so this historical migration keeps
# running after the live `AlertEnrichment` ORM model is removed. Migrations must
# not depend on the current model layer.
_alertenrichment = Table(
    "alertenrichment",
    MetaData(),
    Column("id", String, primary_key=True),
    Column("alert_fingerprint", String, unique=True),
    Column("enrichments", JSON().with_variant(PG_JSONB, "postgresql")),
)


def _json_extract(session, base_field, key):
    """Frozen copy of db_utils.get_json_extract_field, for the same reason the
    table below is reflected locally."""
    if session.bind.dialect.name == "postgresql":
        return type_coerce(base_field, PG_JSONB)[key].astext
    elif session.bind.dialect.name == "mysql":
        return func.json_unquote(func.json_extract(base_field, "$.{}".format(key)))
    else:
        return func.json_extract(base_field, "$.{}".format(key))


def populate_db():
    session = Session(op.get_bind())

    enrichments_col = _alertenrichment.c.enrichments
    dismissed_field = _json_extract(session, enrichments_col, "dismissed")
    status_field = _json_extract(session, enrichments_col, "status")

    rows = session.execute(
        _alertenrichment.select().where(
            and_(
                dismissed_field.in_(["true", "True"]),
                status_field.is_(null()),
            )
        )
    ).all()

    for row in rows:
        updated = dict(row.enrichments or {})
        updated["status"] = _SUPPRESSED
        session.execute(
            update(_alertenrichment)
            .where(_alertenrichment.c.id == row.id)
            .values(enrichments=updated)
        )
    session.commit()

def upgrade() -> None:
    populate_db()


def downgrade() -> None:
    pass

