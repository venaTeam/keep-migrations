"""
Tests for the advisory lock the migration Job runs under.

The Job should be the only writer, so the lock is a backstop rather than the
mechanism it was in the gateway: a retried Job or a concurrent Argo sync would
otherwise attempt the same DDL and lose with "already exists". Unlike the
gateway's version it never continues without the lock -- see
`test_lock_wait_is_bounded_and_then_fails`.

Also covers `get_db_revision`, which must distinguish "no alembic_version table"
from "cannot reach the database".

`test_script_head_is_readable_from_the_shipped_migrations` is the cheap guard on
the lineage itself -- two heads make `upgrade head` undefined, and a broken
`script_location` makes every command fail from inside the image.
"""

import pytest
from unittest.mock import MagicMock, patch

from keep_migrations import runtime


def _engine(dialect="postgresql"):
    engine = MagicMock()
    engine.dialect.name = dialect
    return engine


def _conn_returning(*try_lock_results):
    """A connection whose pg_try_advisory_lock returns the given sequence."""
    conn = MagicMock()
    results = list(try_lock_results)

    def execute(statement, params=None):
        result = MagicMock()
        if "pg_try_advisory_lock" in str(statement):
            result.scalar.return_value = results.pop(0)
        else:
            result.scalar.return_value = True
        return result

    conn.execute.side_effect = execute
    return conn


def _executed_sql(conn):
    return [str(call.args[0]) for call in conn.execute.call_args_list]


def _with_engine(engine):
    return patch.object(runtime, "get_engine", return_value=engine)


def test_lock_is_taken_and_released(monkeypatch):
    conn = _conn_returning(True)
    engine = _engine()
    engine.connect.return_value = conn

    with _with_engine(engine), runtime.migration_lock() as acquired:
        assert acquired is True

    sql = _executed_sql(conn)
    assert any("pg_try_advisory_lock" in s for s in sql)
    assert any("pg_advisory_unlock" in s for s in sql)
    conn.close.assert_called_once()


def test_lock_is_released_even_if_the_migration_fails(monkeypatch):
    conn = _conn_returning(True)
    engine = _engine()
    engine.connect.return_value = conn

    try:
        with _with_engine(engine), runtime.migration_lock():
            raise RuntimeError("migration blew up")
    except RuntimeError:
        pass

    assert any("pg_advisory_unlock" in s for s in _executed_sql(conn))


def test_second_process_waits_for_the_lock(monkeypatch):
    """The loser polls instead of racing the DDL."""
    conn = _conn_returning(False, False, True)
    engine = _engine()
    engine.connect.return_value = conn
    monkeypatch.setattr(runtime, "MIGRATION_LOCK_POLL_SECONDS", 0)

    with _with_engine(engine), runtime.migration_lock() as acquired:
        assert acquired is True

    attempts = [s for s in _executed_sql(conn) if "pg_try_advisory_lock" in s]
    assert len(attempts) == 3


def test_lock_wait_is_bounded_and_then_fails(monkeypatch):
    """A stuck holder must not hang the Job past `activeDeadlineSeconds` -- but
    timing out means failing, not migrating anyway. Whoever holds the lock is
    mid-DDL, and a second run of the same migrations is the collision the lock
    exists to prevent. A failed PreSync stops the sync; the pods keep serving."""
    conn = _conn_returning(*([False] * 50))
    engine = _engine()
    engine.connect.return_value = conn
    monkeypatch.setattr(runtime, "MIGRATION_LOCK_POLL_SECONDS", 0)
    monkeypatch.setattr(runtime, "MIGRATION_LOCK_TIMEOUT", 0)

    with _with_engine(engine), pytest.raises(runtime.LockUnavailable):
        with runtime.migration_lock():
            raise AssertionError("the body must not run without the lock")

    conn.close.assert_called_once()


def test_lock_error_fails_the_run(monkeypatch):
    """If the lock call itself errors we cannot prove exclusivity, so we stop.
    The gateway used to continue here because a pod that refuses to start is
    worse than the risk; a Job has no such constraint."""
    conn = MagicMock()
    conn.execute.side_effect = Exception("no advisory locks here")
    engine = _engine()
    engine.connect.return_value = conn

    with _with_engine(engine), pytest.raises(runtime.LockUnavailable):
        with runtime.migration_lock():
            raise AssertionError("the body must not run without the lock")


def test_non_postgres_dialect_skips_the_lock(monkeypatch):
    engine = _engine(dialect="sqlite")

    with _with_engine(engine), runtime.migration_lock() as acquired:
        assert acquired is True

    engine.connect.assert_not_called()


def test_script_head_is_readable_from_the_shipped_migrations():
    """Guards the absolute script_location and the single head at once: this
    fails when `alembic heads` would return two lines."""
    script = runtime.script_directory()
    assert script is not None
    assert len(script.get_heads()) == 1


def _inspector(has_alembic_version):
    inspector = MagicMock()
    inspector.has_table.return_value = has_alembic_version
    return patch.object(runtime, "sa_inspect", return_value=inspector)


def test_no_alembic_version_table_is_a_new_database():
    engine = _engine()
    with _with_engine(engine), _inspector(False):
        assert runtime.get_db_revision() is None
    engine.connect.assert_not_called()


def test_empty_alembic_version_is_a_new_database():
    conn = MagicMock()
    conn.execute.return_value.first.return_value = None
    engine = _engine()
    engine.connect.return_value.__enter__.return_value = conn
    with _with_engine(engine), _inspector(True):
        assert runtime.get_db_revision() is None


def test_stamped_revision_is_returned():
    conn = MagicMock()
    conn.execute.return_value.first.return_value = ("rev2",)
    engine = _engine()
    engine.connect.return_value.__enter__.return_value = conn
    with _with_engine(engine), _inspector(True):
        assert runtime.get_db_revision() == "rev2"


def test_a_connection_error_is_not_reported_as_a_new_database():
    """The dangerous case. Swallowing this returned None, which reads as "fresh
    database" and answers with a full upgrade from base -- against a database
    that may be entirely populated, and that we could not even reach to check."""
    engine = _engine()
    inspector = MagicMock()
    inspector.has_table.side_effect = OSError("connection refused")
    with _with_engine(engine), patch.object(
        runtime, "sa_inspect", return_value=inspector
    ), pytest.raises(OSError, match="connection refused"):
        runtime.get_db_revision()


def test_a_permission_error_is_not_reported_as_a_new_database():
    conn = MagicMock()
    conn.execute.side_effect = Exception("permission denied for table alembic_version")
    engine = _engine()
    engine.connect.return_value.__enter__.return_value = conn
    with _with_engine(engine), _inspector(True), pytest.raises(
        Exception, match="permission denied"
    ):
        runtime.get_db_revision()
