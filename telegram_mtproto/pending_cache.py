"""In-memory cache of the last known real pending-request counts, plus the
background loop that refreshes it.

A screen that renders must never wait on the network, so it only ever calls
get_cached_pending_count() below; fetch_pending_counts() (the only thing
that actually talks to Telegram) is called exclusively from the refresh
loop here.
"""
import asyncio
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

from sqlalchemy import select

from config import settings
from database.session import AsyncSessionLocal
from database.models import Group
from logs import get_logger
from telegram_mtproto.pending_counts import fetch_pending_counts, mtproto_available

logger = get_logger(__name__)

# chat_id -> (last known value or None, when it was taken)
_cache: Dict[int, Tuple[Optional[int], datetime]] = {}


def get_cached_pending_count(chat_id: int) -> Optional[int]:
    """Returns the last known real count for this chat, or None if there is
    no value yet, the last fetch for it failed, or the value is too stale
    to show."""
    entry = _cache.get(chat_id)
    if entry is None:
        return None
    value, taken_at = entry
    if value is None:
        return None
    max_age_seconds = max(settings.telegram_pending_refresh_seconds * 2, 60)
    age_seconds = (datetime.now(timezone.utc) - taken_at).total_seconds()
    if age_seconds > max_age_seconds:
        return None
    return value


async def _active_group_chat_ids() -> list:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Group.chat_id).where(Group.is_active == True, Group.is_banned == False)  # noqa: E712
        )
        return [row[0] for row in result.all()]


async def _refresh_once() -> None:
    chat_ids = await _active_group_chat_ids()
    if not chat_ids:
        return
    counts = await fetch_pending_counts(chat_ids)
    now = datetime.now(timezone.utc)
    for chat_id, value in counts.items():
        _cache[chat_id] = (value, now)


async def start_pending_refresh_loop(interval_seconds: Optional[int] = None) -> None:
    """Runs the refresh loop forever. A no-op (logs once and returns) when
    the feature is off, so startup never has to check this itself."""
    if not mtproto_available():
        logger.info(
            "Real pending-count feature disabled "
            "(telethon not installed, or TELEGRAM_API_ID/TELEGRAM_API_HASH not configured)"
        )
        return
    if interval_seconds is None:
        interval_seconds = settings.telegram_pending_refresh_seconds

    logger.info("Starting Telegram pending-count refresh loop...", interval_seconds=interval_seconds)
    while True:
        try:
            await _refresh_once()
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Error in Telegram pending-count refresh loop", error=str(e))
        await asyncio.sleep(interval_seconds)
