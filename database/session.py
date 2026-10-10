from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from config import settings
import os

os.makedirs("data", exist_ok=True)

engine = create_async_engine(
    settings.database_url,
    echo=False,
    connect_args={"check_same_thread": False} if "sqlite" in settings.database_url else {},  # pylint: disable=unsupported-membership-test
)


def configure_sqlite_pragmas(target_engine, busy_timeout_ms: int = 30000) -> None:
    """Relaxes SQLite's single-writer lock for this app's concurrent async
    writers (heartbeat loop, cleanup loop, update handlers all write from
    separate connections to the same file). Without this, SQLite's default
    rollback journal plus a ~5s busy timeout makes one writer's transaction
    (e.g. one held open across a slow Telegram API call) immediately fail
    other writers with "database is locked" instead of letting them wait.
    WAL lets readers proceed without blocking on a writer; the longer
    busy_timeout makes a blocked writer wait instead of raising.
    """
    @event.listens_for(target_engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, connection_record):  # pylint: disable=unused-argument
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        cursor.close()


if "sqlite" in settings.database_url:  # pylint: disable=unsupported-membership-test
    configure_sqlite_pragmas(engine)

AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
        finally:
            await session.close()
