"""Regression test for SQLite write-lock contention.

Production log: multiple concurrent async writers (the heartbeat loop, the
verification cleanup loop, and update handlers) all write to the same SQLite
file. With SQLite's default rollback journal and default ~5s busy timeout,
one writer whose transaction stays open for a while (e.g. across a slow
Telegram API call) makes every other writer fail immediately with
"database is locked" instead of waiting - exactly what hit the heartbeat
loop in production ("sqlite3.OperationalError) database is locked").
"""
import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from database.session import configure_sqlite_pragmas


@pytest.mark.asyncio
async def test_configure_sqlite_pragmas_enables_wal_and_busy_timeout(tmp_path):
    db_file = tmp_path / "pragma_test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    configure_sqlite_pragmas(engine, busy_timeout_ms=30000)

    async with engine.connect() as conn:
        journal_mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
        busy_timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()

    await engine.dispose()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 30000


@pytest.mark.asyncio
async def test_production_engine_has_wal_and_busy_timeout_configured():
    from database.session import engine as production_engine

    async with production_engine.connect() as conn:
        journal_mode = (await conn.exec_driver_sql("PRAGMA journal_mode")).scalar()
        busy_timeout = (await conn.exec_driver_sql("PRAGMA busy_timeout")).scalar()

    assert journal_mode.lower() == "wal"
    assert busy_timeout == 30000
