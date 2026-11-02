"""Run the actual additive B6 migration against existing and new rows."""

import importlib.util
from pathlib import Path

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_coverage_defaults_upgrade_and_disposable_downgrade():
    path = Path(__file__).parents[1] / "migrations/versions/2026-09-22-00-00_automation_coverage.py"
    spec = importlib.util.spec_from_file_location("coverage_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE lastalert (fingerprint VARCHAR PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO lastalert VALUES ('existing')"))
        with Operations.context(MigrationContext.configure(conn)):
            migration.upgrade()
        conn.execute(sa.text("INSERT INTO lastalert (fingerprint) VALUES ('new')"))
        table = sa.Table("lastalert", sa.MetaData(), autoload_with=conn)
        assert table.c.automation_matched.nullable is False
        assert table.c.grace_seconds.nullable is True
        assert isinstance(table.c.automation_matched.type, sa.Boolean)
        assert isinstance(table.c.grace_seconds.type, sa.Integer)
        assert conn.execute(sa.select(table.c.automation_matched, table.c.grace_seconds)).all() == [(False, None), (False, None)]
        conn.execute(table.update().where(table.c.fingerprint == "existing").values(automation_matched=True, grace_seconds=120))
        assert conn.execute(sa.select(table.c.grace_seconds).where(table.c.fingerprint == "existing")).scalar() == 120
        with Operations.context(MigrationContext.configure(conn)):
            migration.downgrade()
        assert [column["name"] for column in sa.inspect(conn).get_columns("lastalert")] == ["fingerprint"]
        assert conn.execute(sa.text("SELECT count(*) FROM lastalert")).scalar() == 2
    engine.dispose()
