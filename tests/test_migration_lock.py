"""
Tests for the advisory lock the migration Job runs under.

The Job should be the only writer, so the lock is a backstop rather than the
mechanism it was in the gateway: a retried Job, a concurrent Argo sync, or a pod
whose `SKIP_DB_CREATION` is unset would otherwise attempt the same DDL and lose
with "already exists".

`test_script_head_is_readable_from_the_shipped_migrations` is the cheap guard on
the lineage itself -- two heads make `upgrade head` undefined, and a broken
`script_location` makes every command fail from inside the image.
"""

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


def test_lock_wait_is_bounded(monkeypatch):
    """A stuck holder must not hang the Job past `activeDeadlineSeconds`: after
    the timeout we proceed without the lock rather than block."""
    conn = _conn_returning(*([False] * 50))
    engine = _engine()
    engine.connect.return_value = conn
    monkeypatch.setattr(runtime, "MIGRATION_LOCK_POLL_SECONDS", 0)
    monkeypatch.setattr(runtime, "MIGRATION_LOCK_TIMEOUT", 0)

    with _with_engine(engine), runtime.migration_lock() as acquired:
        assert acquired is False

    assert not any("pg_advisory_unlock" in s for s in _executed_sql(conn))


def test_lock_error_does_not_block_the_run(monkeypatch):
    """A database that refuses the lock call still gets migrated -- refusing to
    migrate because the backstop is unavailable would be the worse failure."""
    conn = MagicMock()
    conn.execute.side_effect = Exception("no advisory locks here")
    engine = _engine()
    engine.connect.return_value = conn

    with _with_engine(engine), runtime.migration_lock() as acquired:
        assert acquired is False


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
