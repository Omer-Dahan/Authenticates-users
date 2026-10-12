"""Background cleanup tasks for expired verification sessions."""
import asyncio
from datetime import datetime, timezone
from aiogram import Bot
from sqlalchemy import select
from database.session import AsyncSessionLocal
from database.models import (
    VerificationSession, JoinRequest, DecisionEnum, ModerationLog, Group, TelegramUser,
    OutageLog,
)
from moderation.actions import decline_expired_request, record_deleted_account_ban
from tracking.channel import send_decision
from logs import get_logger

logger = get_logger(__name__)

# Backoff state for sessions whose clearing attempt keeps failing on Telegram
# (a fully deleted account that even the ban fallback cannot touch, or a
# requester approved elsewhere so the decline is refused for some other
# reason). Kept in-process rather than on the row: this loop runs in a single
# process, and a restart resetting the backoff only ever costs one extra hot
# retry, never an unbounded one. Keyed by VerificationSession.id, value is
# (consecutive_failures, last_attempt_at). The row and session themselves
# always stay open on failure - this only throttles HOW OFTEN Telegram is
# asked again, it never gives up and never closes anything on its own.
#
# A session can also leave the open set through a completely different path -
# a manual approval, a ban, the purge, the reconciliation sweep, or a
# verification that simply completes - none of which know this map exists or
# clear its entry. _prune_resolved_backoff_entries below drops those entries
# every tick, so the map only ever holds sessions that are both still open
# AND still stuck specifically on this loop's own clearing attempt - it does
# not, by itself, bound how many sessions can be stuck at once.
_CLEAR_RETRY_STATE: dict[int, tuple[int, datetime]] = {}
_CLEAR_RETRY_BASE_SECONDS = 60      # the loop's own tick interval
_CLEAR_RETRY_MAX_SECONDS = 3600     # cap: never wait longer than an hour between attempts


async def _prune_resolved_backoff_entries(db, still_open_ids: set[int]) -> None:
    """Drops backoff entries for sessions no longer part of this run's open
    set - either because another path already resolved them (completed_at is
    now set) or because the session row itself is simply gone. Sessions still
    legitimately open but not yet due for a retry are in still_open_ids and
    are never touched here."""
    stale_ids = [sid for sid in list(_CLEAR_RETRY_STATE) if sid not in still_open_ids]
    if not stale_ids:
        return
    result = await db.execute(
        select(VerificationSession.id).where(
            VerificationSession.id.in_(stale_ids),
            VerificationSession.completed_at.is_(None),
        )
    )
    still_open_in_db = set(result.scalars().all())
    for sid in stale_ids:
        if sid not in still_open_in_db:
            _CLEAR_RETRY_STATE.pop(sid, None)


def _clear_retry_due(session_id: int, now: datetime) -> bool:
    """True once enough time has passed since the last failed attempt for
    this session. The delay doubles with each consecutive failure, capped at
    _CLEAR_RETRY_MAX_SECONDS, so a permanently stuck row goes from up to
    three Telegram calls a minute down to at most one call per hour instead
    of retrying forever at full speed."""
    failures, last_attempt = _CLEAR_RETRY_STATE.get(session_id, (0, None))
    if last_attempt is None:
        return True
    backoff = min(_CLEAR_RETRY_BASE_SECONDS * (2 ** (failures - 1)), _CLEAR_RETRY_MAX_SECONDS)
    return (now - last_attempt).total_seconds() >= backoff


async def _is_in_unreconciled_outage(expires_at, db) -> bool:
    """True if expires_at falls inside an outage window whose recovery sweep
    hasn't finished yet - declining it now could reject a user the sweep is
    about to approve, or extend past its (still growing) actual duration."""
    if expires_at is None:
        return False
    result = await db.execute(select(OutageLog).where(OutageLog.ended_at.is_(None)))
    for outage in result.scalars().all():
        if expires_at >= outage.started_at:
            return True
    return False


async def cleanup_expired_sessions(bot: Bot) -> None:
    """Finds all expired pending verification sessions, declines their join requests on Telegram,
    and updates their status in the database without banning them.
    """
    async with AsyncSessionLocal() as db:
        now = datetime.now(timezone.utc)

        # Select all active verification sessions that have expired
        stmt = (
            select(VerificationSession)
            .where(
                VerificationSession.completed_at.is_(None),
                VerificationSession.expires_at < now
            )
        )
        result = await db.execute(stmt)
        expired_sessions = result.scalars().all()

        await _prune_resolved_backoff_entries(db, {s.id for s in expired_sessions})

        if not expired_sessions:
            return

        logger.info("Found expired verification sessions to clean up", count=len(expired_sessions))

        for session in expired_sessions:
            if await _is_in_unreconciled_outage(session.expires_at, db):
                logger.info(
                    "Skipping expired session inside an unreconciled outage window",
                    user_id=session.user_id, chat_id=session.chat_id,
                )
                continue

            user_id = session.user_id
            chat_id = session.chat_id

            # Find the corresponding pending join request. Looked up before
            # anything is written: neither the session nor the join request
            # is marked closed until the Telegram attempt below actually
            # proves the request is gone, so a failed attempt leaves both
            # rows exactly as they were for the next loop iteration to retry.
            jr_stmt = (
                select(JoinRequest)
                .where(
                    JoinRequest.user_id == user_id,
                    JoinRequest.chat_id == chat_id,
                    JoinRequest.decision == DecisionEnum.pending
                )
                .order_by(JoinRequest.created_at.desc())
            )
            jr_result = await db.execute(jr_stmt)
            join_req = jr_result.scalars().first()

            if not join_req:
                # Nothing on Telegram to retry for this session: safe to
                # close it right away.
                session.completed_at = now
                session.passed = False
                logger.warning("No pending join request found for expired session", user_id=user_id, chat_id=chat_id)
                continue

            if not _clear_retry_due(session.id, now):
                # Still backed off from a recent failed attempt on this exact
                # session - skip it quietly this tick, see _CLEAR_RETRY_STATE.
                continue

            # Clear the request on Telegram - decline, escalating to a ban
            # only once the account is proven deleted (see
            # decline_expired_request). A refused decline must never be
            # treated as if it worked.
            clear = await decline_expired_request(bot, chat_id, user_id)
            if not clear.cleared:
                failures = _CLEAR_RETRY_STATE.get(session.id, (0, None))[0] + 1
                _CLEAR_RETRY_STATE[session.id] = (failures, now)
                next_backoff = min(_CLEAR_RETRY_BASE_SECONDS * (2 ** (failures - 1)), _CLEAR_RETRY_MAX_SECONDS)
                logger.warning(
                    "Failed to clear join request on Telegram, leaving the session and row open; "
                    "backing off before the next retry",
                    user_id=user_id, chat_id=chat_id, error=clear.decline_error,
                    consecutive_failures=failures, next_retry_in_seconds=next_backoff,
                )
                continue

            # Cleared: drop any backoff state, the row is being closed now.
            _CLEAR_RETRY_STATE.pop(session.id, None)

            # Only now, with Telegram's outcome confirmed, close both rows.
            session.completed_at = now
            session.passed = False

            if clear.method == "ban":
                logger.info(
                    "Decline was refused for a deactivated account; cleared via ban instead",
                    user_id=user_id, chat_id=chat_id, decline_error=clear.decline_error,
                )
            else:
                logger.info("Declined join request due to verification timeout", user_id=user_id, chat_id=chat_id)

            join_req.decision = DecisionEnum.cleared_via_ban if clear.method == "ban" else DecisionEnum.rejected
            join_req.resolved_at = now

            # Fetch group details for the ban record and for tracking. Looked
            # up by chat_id, not join_req.group_id: chat_id is the value this
            # loop already has in hand and is the group's stable identity, so
            # this can only come back empty if the group is genuinely not
            # tracked in the database at all.
            group_stmt = select(Group).where(Group.chat_id == chat_id)
            group_result = await db.execute(group_stmt)
            group = group_result.scalar_one_or_none()

            if clear.method == "ban":
                if group is not None:
                    await record_deleted_account_ban(
                        db, group.id, user_id,
                        "חשבון מחוק, הדחייה סורבה (חשבון מושבת) בזמן פקיעת האימות, נוקה באמצעות חסימה",
                        "בוט (פקיעת אימות)",
                    )
                else:
                    # The ban already succeeded on Telegram (that is the only
                    # way clear.method can be "ban") - without a tracked
                    # Group row there is no group_id to record it against,
                    # so this is surfaced loudly instead of vanishing with no
                    # trace anywhere.
                    logger.error(
                        "Ban succeeded on Telegram during verification timeout but no Group row "
                        "exists to record it against",
                        user_id=user_id, chat_id=chat_id,
                    )

            # Fetch user details for logging and tracking
            user_stmt = select(TelegramUser).where(TelegramUser.telegram_id == user_id)
            user_result = await db.execute(user_stmt)
            user_record = user_result.scalar_one_or_none()

            username = user_record.username if user_record else None
            first_name = user_record.first_name if user_record else ""
            last_name = user_record.last_name if user_record else ""
            user_name = f"{first_name} {last_name}".strip() or str(user_id)

            # Log the moderation decision
            db.add(ModerationLog(
                group_id=join_req.group_id,
                join_request_id=join_req.id,
                user_id=user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
                decision=join_req.decision,
                score=join_req.score,
                matched_rules=join_req.matched_rules,
                details={"verification_timeout": True, "cleared_via": clear.method},
            ))

            # Send notification to user about the timeout
            try:
                await bot.send_message(
                    user_id,
                    "⏰ תוקף האימות פג. שלח בקשת הצטרפות חדשה."
                )
            except Exception as e:
                logger.debug("Failed to send timeout warning message to user", user_id=user_id, error=str(e))

            # Send decision to tracking channel
            if group:
                matched_dicts = [
                    {"rule_id": "timeout", "rule_name": "Verification Timeout", "score": 0.0}
                ]
                await send_decision(
                    bot,
                    DecisionEnum.rejected,
                    user_id,
                    user_name,
                    username,
                    join_req.score,
                    matched_dicts,
                    group.title or str(chat_id),
                    group.username,
                    reason="verification timeout"
                )

        await db.commit()


async def start_cleanup_loop(
    bot: Bot,
    interval_seconds: int = 60,
    recovery_done_event: "asyncio.Event | None" = None,
    safety_timeout_seconds: int = 120,
) -> None:
    """Runs a background loop that periodically cleans up expired verification sessions.

    If recovery_done_event is given, the first run waits for the outage
    recovery sweep to finish reconciling leftovers first, so it can't decline a
    session the sweep is about to resolve or extend. Bounded by
    safety_timeout_seconds so a stuck sweep can never block cleanup forever.
    """
    logger.info("Starting background verification cleanup loop...")
    if recovery_done_event is not None:
        try:
            await asyncio.wait_for(recovery_done_event.wait(), timeout=safety_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("Recovery sweep did not finish before safety timeout; starting cleanup anyway")
    while True:
        try:
            await cleanup_expired_sessions(bot)
        except Exception as e:
            logger.error("Error in verification cleanup loop", error=str(e))
        await asyncio.sleep(interval_seconds)
