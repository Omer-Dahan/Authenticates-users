"""Helpers for the single-row BotState table used to detect outages on startup."""
from datetime import datetime, timezone
from typing import Optional, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from config import settings
from database.models import BotState, OutageLog
from logs import get_logger

logger = get_logger(__name__)

# Telegram only retains undelivered updates for 24h, so an extension can never
# be worth more than that even if the bot was down longer.
MAX_OUTAGE_EXTENSION_SECONDS = 86400


async def touch_heartbeat(db: AsyncSession) -> None:
    """Records that the bot is alive right now, seeding BotState if absent."""
    now = datetime.now(timezone.utc)
    state = await db.get(BotState, 1)
    if state is None:
        db.add(BotState(id=1, last_heartbeat_at=now, last_started_at=now))
    else:
        state.last_heartbeat_at = now
    await db.commit()


async def detect_outage(db: AsyncSession) -> Tuple[Optional[datetime], float]:
    """Compares now against the last recorded heartbeat to detect a downtime gap.

    Returns (outage_started_at, outage_seconds) when an outage is detected,
    or (None, 0.0) on first run or when the gap is below the threshold.
    Always seeds/refreshes BotState as a side effect.
    """
    now = datetime.now(timezone.utc)
    state = await db.get(BotState, 1)

    if state is None or state.last_heartbeat_at is None:
        if state is None:
            db.add(BotState(id=1, last_heartbeat_at=now, last_started_at=now))
        else:
            state.last_heartbeat_at = now
            state.last_started_at = now
        await db.commit()
        return None, 0.0

    last_heartbeat = state.last_heartbeat_at
    if last_heartbeat.tzinfo is None:
        last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)

    gap_seconds = (now - last_heartbeat).total_seconds()
    state.last_started_at = now

    if gap_seconds > settings.outage_threshold_seconds:
        outage_seconds = min(gap_seconds, MAX_OUTAGE_EXTENSION_SECONDS)
        state.outage_started_at = last_heartbeat
        state.outage_seconds = outage_seconds
        state.recovery_completed_at = None
        state.last_heartbeat_at = now
        # Open the OutageLog row here, before polling starts, so that replayed
        # chat_member updates (processed within seconds of start_polling) find
        # an open outage window. The sweep below only finalizes this row - it
        # never has to create it on the primary path.
        db.add(OutageLog(
            started_at=last_heartbeat,
            duration_seconds=outage_seconds,
            groups_scanned=0, requests_recovered=0,
            sessions_extended=0, members_messaged=0, skipped=0,
            details={},
        ))
        await db.commit()
        logger.warning(
            "Outage detected on startup", gap_seconds=gap_seconds, outage_seconds=outage_seconds,
        )
        return last_heartbeat, outage_seconds

    state.last_heartbeat_at = now
    await db.commit()
    return None, 0.0
