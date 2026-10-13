"""Read-only MTProto reader for the real pending-join-request count.

Telegram's Bot API never exposes how many join requests are actually
pending in a chat (the bot only learns about one when it is created, and
nothing tells it when an admin approves or declines one inside the app).
ChannelFull.requests_pending, read over MTProto with the bot's own token,
does carry that real number - this module only ever reads it.

This client must NEVER process updates and NEVER approve, decline, ban, or
otherwise write anything. It only ever issues GetFullChannelRequest.
"""
import asyncio
from typing import Dict, Iterable, Optional

from config import settings
from logs import get_logger

logger = get_logger(__name__)

try:
    from telethon import TelegramClient, functions
    _TELETHON_AVAILABLE = True
except ImportError:
    TelegramClient = None  # type: ignore[assignment]
    functions = None  # type: ignore[assignment]
    _TELETHON_AVAILABLE = False

SESSION_PATH = "data/mtproto_bot.session"
_FETCH_TIMEOUT_SECONDS = 15

_client = None
_client_lock = asyncio.Lock()


def mtproto_available() -> bool:
    """Whether the real-pending-count feature can run at all: the telethon
    package is importable, the feature is not turned off, and the
    credentials it needs are configured."""
    return (
        _TELETHON_AVAILABLE
        and settings.telegram_pending_enabled
        and settings.telegram_api_id is not None
        and bool(settings.telegram_api_hash)
    )


async def _get_client():
    global _client
    async with _client_lock:
        if _client is None:
            client = TelegramClient(SESSION_PATH, settings.telegram_api_id, settings.telegram_api_hash)
            await client.start(bot_token=settings.bot_token)
            _client = client
        return _client


async def close_client() -> None:
    """Disconnects the MTProto client cleanly, if one was ever opened. Never
    raises: a shutdown must never be blocked by this feature."""
    global _client
    async with _client_lock:
        if _client is not None:
            try:
                await _client.disconnect()
            except Exception as e:  # pylint: disable=broad-except
                logger.warning("Error disconnecting MTProto pending-count client", error=str(e))
            _client = None


async def fetch_pending_counts(chat_ids: Iterable[int]) -> Dict[int, Optional[int]]:
    """Reads ChannelFull.requests_pending for every given chat id.

    One GetFullChannelRequest per chat, read-only, never raising to the
    caller: a client-level failure (disabled feature, bad credentials, no
    connection) yields None for every chat, and a per-chat error (access
    restricted, small group with no such field, timeout) yields None for
    that chat only.
    """
    chat_ids = list(chat_ids)
    results: Dict[int, Optional[int]] = {chat_id: None for chat_id in chat_ids}
    if not chat_ids or not mtproto_available():
        return results

    try:
        client = await asyncio.wait_for(_get_client(), timeout=_FETCH_TIMEOUT_SECONDS)
    except Exception as e:  # pylint: disable=broad-except
        logger.warning("MTProto pending-count client unavailable", error=str(e))
        return results

    for chat_id in chat_ids:
        try:
            full = await asyncio.wait_for(
                client(functions.channels.GetFullChannelRequest(channel=chat_id)),
                timeout=_FETCH_TIMEOUT_SECONDS,
            )
            results[chat_id] = getattr(full.full_chat, "requests_pending", None)
        except Exception as e:  # pylint: disable=broad-except
            logger.warning("MTProto pending-count fetch failed for one chat", chat_id=chat_id, error=str(e))
            results[chat_id] = None

    return results
