"""Tracks chat_member add/remove events and catches up on ones missed during an outage."""
from datetime import datetime, timezone, timedelta
from typing import Optional

from aiogram import Router, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberUpdated
from sqlalchemy import select, or_, and_

from database.session import AsyncSessionLocal
from database.models import (
    Group, GroupConfig, MemberEvent, JoinRequest, DecisionEnum, ModerationLog,
    VerificationSession, OutageLog,
)
from verification import VerificationEngine
from bot.text import esc
from logs import get_logger

logger = get_logger(__name__)
router = Router()

_VERIFICATION_ENGINE: Optional[VerificationEngine] = None

_ALREADY_IN_STATUSES = {"member", "administrator", "creator"}
_LEFT_STATUSES = {"left", "kicked"}


def _is_joined(member) -> bool:
    """Mirrors recovery/sweep.py's already_in check: restricted only counts
    as joined if the user is actually still a member (is_member=True)."""
    if member is None:
        return False
    status = member.status
    return status in _ALREADY_IN_STATUSES or (
        status == "restricted" and getattr(member, "is_member", False)
    )


def setup_engine(ver_engine: VerificationEngine) -> None:
    global _VERIFICATION_ENGINE  # pylint: disable=global-statement
    _VERIFICATION_ENGINE = ver_engine


async def _get_config_value(group_id: int, key: str, default, db) -> bool:
    result = await db.execute(
        select(GroupConfig).where(GroupConfig.group_id == group_id, GroupConfig.key == key)
    )
    cfg = result.scalar_one_or_none()
    if not cfg or cfg.value is None:
        return default
    if cfg.value_type == "bool":
        return cfg.value.lower() in ("true", "1", "yes")
    return cfg.value


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _find_open_outage(event_at: datetime, db) -> Optional[OutageLog]:
    """Finds the outage whose window covers event_at (open windows count as
    covering everything after their start, since their real end isn't known yet)."""
    event_at = _aware(event_at)
    result = await db.execute(select(OutageLog).order_by(OutageLog.started_at.desc()))
    for outage in result.scalars().all():
        started_at = _aware(outage.started_at)
        ended_at = _aware(outage.ended_at)
        if started_at <= event_at and (ended_at is None or event_at <= ended_at):
            return outage
    return None


async def _catch_up_join(bot: Bot, db, group: Group, user_id: int, chat_id: int, outage: OutageLog) -> None:
    # The normal join-request flow only still owns this user if it has an open
    # decision (pending/manual_review) or actually resolved it as approved.
    # A rejected/banned row does not block catch-up: the user is in the group
    # now, so that old decision clearly did not resolve this join.
    existing = await db.execute(
        select(JoinRequest).where(
            JoinRequest.user_id == user_id,
            JoinRequest.chat_id == chat_id,
            or_(
                JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
                and_(JoinRequest.decision == DecisionEnum.approved, JoinRequest.resolved_at.isnot(None)),
            ),
        )
    )
    if existing.scalars().first():
        return  # normal flow already owns this user

    requires_verification = await _get_config_value(group.id, "verification_required", True, db)
    if not requires_verification:
        return

    session_result = await db.execute(
        select(VerificationSession).where(
            VerificationSession.user_id == user_id,
            VerificationSession.chat_id == chat_id,
            VerificationSession.completed_at.is_(None),
        )
    )
    if session_result.scalar_one_or_none():
        return

    question = await _VERIFICATION_ENGINE.get_random_question(group.id, db)
    if not question:
        return

    try:
        await bot.send_message(
            user_id,
            f"🔐 <b>נדרש אימות</b>\n\n{esc(question.question)}\n\n"
            f"יש לך {question.max_attempts} ניסיון/ות ו-{question.timeout_seconds // 60} דקות לענות.",
            parse_mode="HTML",
        )
    except TelegramAPIError as e:
        logger.warning("Could not send catch-up verification question", user_id=user_id, error=str(e))
        return

    now = datetime.now(timezone.utc)
    db.add(VerificationSession(
        user_id=user_id, chat_id=chat_id, question_id=question.id,
        attempts=0, notified_at=now,
        expires_at=now + timedelta(seconds=question.timeout_seconds),
    ))
    outage.members_messaged = (outage.members_messaged or 0) + 1


async def _catch_up_leave(db, group: Group, user_id: int, chat_id: int) -> None:
    now = datetime.now(timezone.utc)

    session_result = await db.execute(
        select(VerificationSession).where(
            VerificationSession.user_id == user_id,
            VerificationSession.chat_id == chat_id,
            VerificationSession.completed_at.is_(None),
        )
    )
    session = session_result.scalar_one_or_none()
    if session:
        session.completed_at = now
        session.passed = False

    jr_result = await db.execute(
        select(JoinRequest).where(
            JoinRequest.user_id == user_id,
            JoinRequest.chat_id == chat_id,
            JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
            JoinRequest.resolved_at.is_(None),
        )
    )
    jr = jr_result.scalars().first()
    if jr:
        jr.decision = DecisionEnum.rejected
        jr.resolved_at = now
        db.add(ModerationLog(
            group_id=group.id, join_request_id=jr.id, user_id=user_id,
            decision=DecisionEnum.rejected, score=jr.score, matched_rules=jr.matched_rules,
            details={"left_during_outage": True, "note": "left during outage"},
        ))


async def _close_requests_resolved_outside(db, group: Group, user_id: int, chat_id: int) -> None:
    now = datetime.now(timezone.utc)
    jr_result = await db.execute(
        select(JoinRequest).where(
            JoinRequest.user_id == user_id,
            JoinRequest.chat_id == chat_id,
            JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
            JoinRequest.resolved_at.is_(None),
        )
    )
    for jr in jr_result.scalars().all():
        jr.decision = DecisionEnum.approved
        jr.resolved_at = now
        jr.notes = "joined, request resolved outside the bot"
        db.add(ModerationLog(
            group_id=group.id, join_request_id=jr.id, user_id=user_id,
            decision=DecisionEnum.approved, score=jr.score, matched_rules=jr.matched_rules,
            details={"resolved_outside_bot": True, "note": "joined while the row was open"},
        ))

    session_result = await db.execute(
        select(VerificationSession).where(
            VerificationSession.user_id == user_id,
            VerificationSession.chat_id == chat_id,
            VerificationSession.completed_at.is_(None),
        )
    )
    for session in session_result.scalars().all():
        session.completed_at = now


@router.chat_member()
async def handle_chat_member_update(update: ChatMemberUpdated, bot: Bot) -> None:
    chat_id = update.chat.id
    user_id = update.new_chat_member.user.id
    old_status = update.old_chat_member.status if update.old_chat_member else None
    new_status = update.new_chat_member.status if update.new_chat_member else None
    event_at = update.date

    async with AsyncSessionLocal() as db:
        group_result = await db.execute(select(Group).where(Group.chat_id == chat_id))
        group = group_result.scalar_one_or_none()
        if not group or not group.is_active or group.is_banned:
            return

        outage = await _find_open_outage(event_at, db)
        during_outage = outage is not None

        db.add(MemberEvent(
            group_id=group.id, chat_id=chat_id, user_id=user_id,
            old_status=old_status, new_status=new_status,
            event_at=event_at, during_outage=during_outage,
        ))

        joined = _is_joined(update.new_chat_member) and not _is_joined(update.old_chat_member)
        left = new_status in _LEFT_STATUSES

        if not during_outage:
            # Outside a detected outage, this is a record plus one reconciliation:
            # an admin approving a request inside the Telegram app sends the bot
            # no join-request update, only this join. A member cannot have a
            # pending request, so any open row for them is closed here (database
            # only). The normal join-request / verification flow still owns any
            # DM to the user.
            if joined:
                await _close_requests_resolved_outside(db, group, user_id, chat_id)
            await db.commit()
            return

        if joined:
            await _catch_up_join(bot, db, group, user_id, chat_id, outage)
        elif left:
            await _catch_up_leave(db, group, user_id, chat_id)

        await db.commit()
