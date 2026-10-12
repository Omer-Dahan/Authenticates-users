"""Recovery sweep: reconciles join requests and verification sessions that were
left dangling by a detected outage, then reports one summary per affected group
to its owner.
"""
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from config import settings
from database.session import AsyncSessionLocal
from database.models import (
    Group, GroupConfig, GroupQuestion, JoinRequest, DecisionEnum, ModerationLog,
    VerificationSession, OutageLog, BotState,
)
from verification.engine import VerificationEngine
from bot.text import esc
from logs import get_logger

logger = get_logger(__name__)

_ALREADY_IN_GROUP_STATUSES = {"creator", "administrator", "member"}


def _question_prompt(question: GroupQuestion) -> str:
    return (
        f"🔐 <b>נדרש אימות</b>\n\n{esc(question.question)}\n\n"
        f"יש לך {question.max_attempts} ניסיון/ות ו-{question.timeout_seconds // 60} דקות לענות."
    )


async def _get_config_value(group_id: int, key: str, default, db) -> float:
    result = await db.execute(
        select(GroupConfig).where(GroupConfig.group_id == group_id, GroupConfig.key == key)
    )
    cfg = result.scalar_one_or_none()
    if not cfg or cfg.value is None:
        return default
    if cfg.value_type == "float":
        try:
            return float(cfg.value)
        except ValueError:
            return default
    return cfg.value


async def _send_safe(bot: Bot, chat_id: int, text: str) -> bool:
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML")
        return True
    except TelegramAPIError as e:
        logger.warning("Recovery: could not deliver message", chat_id=chat_id, error=str(e))
        return False


async def _catch_up_verification(bot, db, ver_engine, group, jr, outage_seconds, summary) -> None:
    now = datetime.now(timezone.utc)
    session_result = await db.execute(
        select(VerificationSession).where(
            VerificationSession.user_id == jr.user_id,
            VerificationSession.chat_id == jr.chat_id,
            VerificationSession.completed_at.is_(None),
        )
    )
    session = session_result.scalar_one_or_none()

    if session:
        if session.notified_at is None:
            question = await db.get(GroupQuestion, session.question_id) if session.question_id else None
            if not question:
                return
            if await _send_safe(bot, jr.user_id, _question_prompt(question)):
                session.notified_at = now
                session.expires_at = now + timedelta(seconds=question.timeout_seconds)
                summary["recovered"] += 1
            else:
                summary["skipped"] += 1
            return

        # Already notified before the outage started. Only touch sessions whose
        # deadline would otherwise have run out unnoticed - once extended,
        # expires_at moves into the future so a second sweep run is a no-op
        # (idempotency), and a session with plenty of time left is untouched.
        expires_at = session.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at is not None and expires_at <= now:
            session.expires_at = expires_at + timedelta(seconds=outage_seconds)
            if await _send_safe(
                bot, jr.user_id,
                "⏰ הבוט היה זמנית לא זמין. הזמן שנותר לך לענות על שאלת האימות הוארך בהתאם.",
            ):
                summary["extended"] += 1
            else:
                summary["skipped"] += 1
        return

    # No session exists at all: send the question and start the countdown now.
    question = await ver_engine.get_random_question(group.id, db)
    if question:
        if not await _send_safe(bot, jr.user_id, _question_prompt(question)):
            summary["skipped"] += 1
            return
        db.add(VerificationSession(
            user_id=jr.user_id,
            chat_id=jr.chat_id,
            question_id=question.id,
            attempts=0,
            notified_at=now,
            expires_at=now + timedelta(seconds=question.timeout_seconds),
        ))
        summary["recovered"] += 1
        return

    # No question configured for this group: fall back exactly like the
    # no-question path in bot/handlers/join_requests.py.
    approve_threshold = await _get_config_value(group.id, "approve_threshold", 60.0, db)
    if jr.score >= approve_threshold:
        try:
            await bot.approve_chat_join_request(jr.chat_id, jr.user_id)
        except TelegramAPIError:
            pass
        jr.decision = DecisionEnum.approved
    else:
        try:
            await bot.decline_chat_join_request(jr.chat_id, jr.user_id)
        except TelegramAPIError:
            pass
        jr.decision = DecisionEnum.rejected
    jr.resolved_at = now
    db.add(ModerationLog(
        group_id=group.id, join_request_id=jr.id, user_id=jr.user_id,
        decision=jr.decision, score=jr.score, matched_rules=jr.matched_rules,
        details={"recovered_during_outage": True, "reason": "no_question_fallback"},
    ))
    summary["recovered"] += 1


async def _process_join_request(bot, db, ver_engine, group, jr, outage_seconds, summary) -> None:
    try:
        member = await bot.get_chat_member(jr.chat_id, jr.user_id)
    except TelegramAPIError as e:
        logger.warning(
            "Recovery: get_chat_member failed", user_id=jr.user_id, chat_id=jr.chat_id, error=str(e),
        )
        summary["skipped"] += 1
        return

    now = datetime.now(timezone.utc)
    status = member.status
    already_in = status in _ALREADY_IN_GROUP_STATUSES or (
        status == "restricted" and getattr(member, "is_member", False)
    )

    if already_in:
        jr.decision = DecisionEnum.approved
        jr.resolved_at = now
        db.add(ModerationLog(
            group_id=group.id, join_request_id=jr.id, user_id=jr.user_id,
            decision=DecisionEnum.approved, score=jr.score, matched_rules=jr.matched_rules,
            details={"recovered_during_outage": True, "reason": "already_member"},
        ))
        summary["recovered"] += 1
        return

    if status == "kicked":
        jr.decision = DecisionEnum.rejected
        jr.resolved_at = now
        db.add(ModerationLog(
            group_id=group.id, join_request_id=jr.id, user_id=jr.user_id,
            decision=DecisionEnum.rejected, score=jr.score, matched_rules=jr.matched_rules,
            details={"recovered_during_outage": True, "reason": "kicked_during_outage"},
        ))
        summary["recovered"] += 1
        return

    # left / not resolvable: fall through to the verification catch-up
    await _catch_up_verification(bot, db, ver_engine, group, jr, outage_seconds, summary)


async def _send_owner_summary(bot, group_title, group_chat_id, owner_id, outage_started_at, outage_seconds, summary) -> None:
    local_start = outage_started_at.strftime("%H:%M")
    minutes = max(1, int(outage_seconds // 60))
    text = (
        f"⚠️ <b>זוהתה תקלת זמינות</b>\n\n"
        f"📍 קבוצה: {esc(group_title) if group_title else group_chat_id}\n"
        f"🕐 שעת ההפסקה: {local_start}\n"
        f"⏱ משך משוער: כ-{minutes} דקות\n\n"
        f"✅ בקשות שהושלמו: {summary['recovered']}\n"
        f"⏰ טיימרים שהוארכו: {summary['extended']}\n"
        f"⚠️ דילוגים עקב שגיאות: {summary['skipped']}"
    )
    await _send_safe(bot, owner_id, text)


async def _sweep(bot: Bot, ver_engine: VerificationEngine, outage_started_at: datetime, outage_seconds: float) -> None:
    async with AsyncSessionLocal() as db:
        # detect_outage() already opens the OutageLog row before polling starts
        # (see recovery/state.py), so the normal path just finalizes the newest
        # open one here. Only create a row if none is open - e.g. a caller that
        # runs the sweep without going through detect_outage() first.
        open_result = await db.execute(
            select(OutageLog).where(OutageLog.ended_at.is_(None)).order_by(OutageLog.started_at.desc())
        )
        outage_log = open_result.scalars().first()
        if outage_log is None:
            outage_log = OutageLog(
                started_at=outage_started_at,
                duration_seconds=outage_seconds,
                groups_scanned=0, requests_recovered=0,
                sessions_extended=0, members_messaged=0, skipped=0,
                details={},
            )
            db.add(outage_log)
            await db.commit()
        outage_log_id = outage_log.id

    groups_scanned = 0
    requests_recovered = 0
    sessions_extended = 0
    skipped = 0
    per_group_details = {}

    try:
        async with AsyncSessionLocal() as db:
            groups_result = await db.execute(
                select(Group).where(Group.is_active == True, Group.is_banned == False)  # noqa: E712
            )
            group_ids = [g.id for g in groups_result.scalars().all()]

        for group_id in group_ids:
            groups_scanned += 1
            summary = {"recovered": 0, "extended": 0, "skipped": 0}

            async with AsyncSessionLocal() as db:
                # Re-fetch the group here so it is bound to this session - the
                # instance from the listing query above is detached once that
                # session closes and must not be carried across sessions.
                group = await db.get(Group, group_id)
                if group is None:
                    continue

                reqs_result = await db.execute(
                    select(JoinRequest).where(
                        JoinRequest.group_id == group.id,
                        JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
                        JoinRequest.resolved_at.is_(None),
                    )
                )
                join_reqs = reqs_result.scalars().all()

                for jr in join_reqs:
                    await _process_join_request(bot, db, ver_engine, group, jr, outage_seconds, summary)

                await db.commit()
                group_title = group.title
                group_chat_id = group.chat_id
                group_owner_id = group.owner_id

            requests_recovered += summary["recovered"]
            sessions_extended += summary["extended"]
            skipped += summary["skipped"]
            per_group_details[str(group_id)] = summary

            if summary["recovered"] or summary["extended"]:
                await _send_owner_summary(
                    bot, group_title, group_chat_id, group_owner_id, outage_started_at, outage_seconds, summary,
                )

        logger.info(
            "Recovery sweep completed",
            groups_scanned=groups_scanned, requests_recovered=requests_recovered,
            sessions_extended=sessions_extended, skipped=skipped,
        )
    finally:
        # Always close the row, even when the loop above raised - an OutageLog
        # left open forever would permanently disable the cleanup loop's
        # expired-session handling (see verification/cleanup.py). Also close
        # any other row still open from a previous run that died mid-sweep,
        # not just the one this run used.
        now = datetime.now(timezone.utc)
        async with AsyncSessionLocal() as db:
            open_result = await db.execute(select(OutageLog).where(OutageLog.ended_at.is_(None)))
            for log in open_result.scalars().all():
                log.ended_at = now
                if log.id == outage_log_id:
                    log.groups_scanned = groups_scanned
                    log.requests_recovered = requests_recovered
                    log.sessions_extended = sessions_extended
                    log.skipped = skipped
                    log.details = {"per_group": per_group_details}

            state = await db.get(BotState, 1)
            if state:
                state.recovery_completed_at = now
            await db.commit()


async def run_recovery_sweep(
    bot: Bot,
    ver_engine: VerificationEngine,
    outage_started_at: Optional[datetime],
    outage_seconds: float,
    grace_seconds: int,
    recovery_done_event: asyncio.Event,
) -> None:
    """Waits out the replay grace period, then reconciles any outage leftovers.

    Always sets recovery_done_event when finished (or immediately if there was
    no outage to begin with) so start_cleanup_loop knows it is safe to decline
    expired sessions.

    Once recovery_done_event is set, also runs the join-request reconciliation
    sweep once per active group (run_daily_reconciliation, the same bounded,
    per-group-try/except sweep the daily loop uses) so approvals, declines, or
    departures that happened while the bot was down are caught right after
    startup instead of waiting for the daily loop's own interval. This runs
    after the event is set so it never delays start_cleanup_loop or the daily
    reconciliation loop from being unblocked, and a failure here is logged and
    swallowed rather than affecting recovery itself.

    settings.reconciliation_daily_sweep_enabled is the master switch for this
    whole quiet sweep, including this always-real (dry_run=False) post-
    recovery pass and the age-based auto-close it can trigger - so it is
    honoured here too, not just in the daily loop. Turning it off only stops
    this quiet startup pass; the on-demand /reconcile command and the
    settings-menu button stay available regardless.
    """
    await asyncio.sleep(grace_seconds)

    if outage_started_at is None:
        recovery_done_event.set()
    else:
        try:
            await _sweep(bot, ver_engine, outage_started_at, outage_seconds)
        except Exception as e:  # pylint: disable=broad-except
            logger.error("Recovery sweep failed", error=str(e))
        finally:
            recovery_done_event.set()

    if not settings.reconciliation_daily_sweep_enabled:
        logger.info("Daily reconciliation sweep disabled via settings; skipping post-recovery reconciliation pass")
        return

    from recovery.reconciliation_loop import run_daily_reconciliation, mark_recovery_sweep_started
    # Set synchronously, before the first await below, so the daily loop -
    # which only decides whether to skip its own first iteration after
    # waiting on recovery_done_event - can never observe this as unset once
    # that event has been seen set (single-threaded asyncio scheduling
    # guarantees this runs before the daily loop's waiter resumes).
    mark_recovery_sweep_started()
    try:
        await run_daily_reconciliation(bot)
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Post-recovery reconciliation sweep failed", error=str(e))
