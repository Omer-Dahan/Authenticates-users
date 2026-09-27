"""Background loop that periodically records bot liveness for outage detection."""
import asyncio

from database.session import AsyncSessionLocal
from recovery.state import touch_heartbeat
from logs import get_logger

logger = get_logger(__name__)


async def start_heartbeat_loop(interval_seconds: int = 60) -> None:
    logger.info("Starting heartbeat loop...", interval_seconds=interval_seconds)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await touch_heartbeat(db)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Error in heartbeat loop", error=str(e))
        await asyncio.sleep(interval_seconds)
