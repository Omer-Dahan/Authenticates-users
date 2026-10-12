"""Handles chat_join_request events - multi-tenant moderation flow."""
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Router, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatJoinRequest

from database.session import AsyncSessionLocal
from database.models import (
    DecisionEnum, TelegramUser, JoinRequest, ModerationLog, Group,
    GroupConfig, VerificationSession,
)
from moderation import ModerationEngine
from verification import VerificationEngine
from bot.keyboards.settings_kb import admin_review_kb
from bot.text import esc
from tracking import send_decision
from sqlalchemy import select
from logs import get_logger

logger = get_logger(__name__)
router = Router()

_MODERATION_ENGINE: Optional[ModerationEngine] = None
_VERIFICATION_ENGINE: Optional[VerificationEngine] = None

# A new request must be strictly newer than the open row by more than this to
# count as genuinely new - absorbs clock skew and routine re-delivery (e.g. the
# outage recovery replaying the backlog) instead of treating it as a supersede.
_SUPERSEDE_MARGIN = timedelta(seconds=60)


def _aware(dt: datetime) -> datetime:
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def setup_engines(mod_engine: ModerationEngine, ver_engine: VerificationEngine) -> None:
    global _MODERATION_ENGINE, _VERIFICATION_ENGINE  # pylint: disable=global-statement
    _MODERATION_ENGINE = mod_engine
    _VERIFICATION_ENGINE = ver_engine


async def _ensure_user(user, db) -> TelegramUser:
    result = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == user.id)
    )
    db_user = result.scalar_one_or_none()
    if not db_user:
        db_user = TelegramUser(
            telegram_id=user.id,
            username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
            language_code=getattr(user, "language_code", None),
        )
        db.add(db_user)
    else:
        db_user.username = user.username
        db_user.first_name = user.first_name
        db_user.last_name = user.last_name
    await db.flush()
    return db_user


async def _get_config_value(group_id: int, key: str, default, db) -> any:
    result = await db.execute(
        select(GroupConfig).where(GroupConfig.group_id == group_id, GroupConfig.key == key)
    )
    cfg = result.scalar_one_or_none()
    if not cfg or cfg.value is None:
        return default
    if cfg.value_type == "bool":
        return cfg.value.lower() in ("true", "1", "yes")
    if cfg.value_type == "float":
        try:
            return float(cfg.value)
        except ValueError:
            return default
    if cfg.value_type == "int":
        try:
            return int(cfg.value)
        except ValueError:
            return default
    return cfg.value


async def _send_message_safe(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except TelegramAPIError as e:
        logger.warning("Could not send message to user", user_id=user_id, error=str(e))


async def _notify_manual_review(
    bot: Bot,
    user,
    group: Group,
    join_req: JoinRequest,
    scoring_result,
) -> None:
    first_name = esc(user.first_name or "")
    last_name = esc(user.last_name or "")
    name = f"{first_name} {last_name}".strip()
    username_str = f"@{esc(user.username)}" if user.username else "no username"
    rules_str = ", ".join(scoring_result.matched_rule_ids) or "-"

    text = (
        f"⚠️ <b>סקירה ידנית נדרשת</b>\n\n"
        f"📍 קבוצה: {esc(group.title)}\n"
        f"👤 {name} ({username_str})\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"📊 ניקוד: {scoring_result.total_score:+.1f}\n"
        f"🔍 כללים: {rules_str}"
    )
    kb = admin_review_kb(user.id, join_req.id, group.id)

    # Send to group owner
    try:
        await bot.send_message(group.owner_id, text, parse_mode="HTML", reply_markup=kb)
    except TelegramAPIError as e:
        logger.error("Failed to notify owner for review", error=str(e))


@router.chat_join_request()
async def handle_join_request(
    request: ChatJoinRequest,
    bot: Bot,
    group_db: Group = None,
    raid_active: bool = False,
) -> None:
    # group_db is injected by GroupCheckMiddleware
    if group_db is None:
        return

    if raid_active:
        logger.warning(
            "Join request received during active raid - applying strict scoring",
            user_id=request.from_user.id,
            chat_id=request.chat.id,
        )

    user = request.from_user
    chat_id = request.chat.id
    group = group_db

    logger.info(
        "Join request received",
        user_id=user.id,
        username=user.username,
        chat_id=chat_id,
        group_db_id=group.id,
    )

    async with AsyncSessionLocal() as db:
        unresolved = await db.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == chat_id,
                JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
                JoinRequest.resolved_at.is_(None),
            )
        )
        unresolved_rows = list(unresolved.scalars().all())

        if unresolved_rows:
            request_date = _aware(request.date)
            known_created_ats = [
                _aware(row.created_at) for row in unresolved_rows if row.created_at is not None
            ]
            # A row with no created_at is treated as ancient rather than fresh:
            # the goal is to unblock users, so an unknown age must never stop a
            # genuinely new request from superseding.
            latest_created_at = max(known_created_ats) if known_created_ats else None

            if latest_created_at is not None and request_date <= latest_created_at + _SUPERSEDE_MARGIN:
                # Not genuinely new - a re-delivery of a request we already have
                # open (e.g. the outage recovery replaying the backlog). Running
                # the flow again would score the user again and notify the admin
                # a second time for what is, from their point of view, the same
                # open request.
                logger.info(
                    "Join request ignored - re-delivery of an unresolved request",
                    user_id=user.id,
                    chat_id=chat_id,
                )
                return

            # Genuinely newer request for a user who already has an open,
            # unresolved request - the old row was abandoned (e.g. declined
            # manually inside Telegram) and must not keep blocking this user
            # forever. Close it as superseded and fall through to process the
            # new request normally.
            now = datetime.now(timezone.utc)
            for old_row in unresolved_rows:
                old_row.decision = DecisionEnum.rejected
                old_row.resolved_at = now
                old_row.notes = "superseded by a newer join request"
                db.add(ModerationLog(
                    group_id=old_row.group_id, join_request_id=old_row.id,
                    user_id=user.id, username=user.username,
                    first_name=user.first_name, last_name=user.last_name,
                    decision=DecisionEnum.rejected, score=old_row.score,
                    matched_rules=old_row.matched_rules,
                    details={
                        "superseded_by_new_request": True,
                        "superseded_row_id": old_row.id,
                        "new_request_at": request_date.isoformat(),
                    },
                ))

            open_sessions = await db.execute(
                select(VerificationSession).where(
                    VerificationSession.user_id == user.id,
                    VerificationSession.chat_id == chat_id,
                    VerificationSession.completed_at.is_(None),
                )
            )
            for session in open_sessions.scalars().all():
                session.completed_at = now
                session.passed = False

            await db.flush()
            logger.info(
                "Unresolved join request(s) superseded by a newer request",
                user_id=user.id,
                chat_id=chat_id,
                superseded_count=len(unresolved_rows),
            )

        await _ensure_user(user, db)

        user_data = {
            "user_id": user.id,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
        }

        scoring_result = await _MODERATION_ENGINE.evaluate(
            user_data, db, group_id=group.id, raid_active=raid_active
        )

        join_req = JoinRequest(
            group_id=group.id,
            user_id=user.id,
            chat_id=chat_id,
            decision=DecisionEnum.pending,
            score=scoring_result.total_score,
            matched_rules=scoring_result.matched_rule_ids,
        )
        db.add(join_req)
        await db.flush()

        user_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or str(user.id)
        matched_rules_dicts = [
            {"rule_id": r.rule_id, "rule_name": r.rule_name, "score": r.score}
            for r in scoring_result.matched_rules
        ]

        if scoring_result.decision == DecisionEnum.banned:
            try:
                await bot.ban_chat_member(chat_id, user.id)
                await bot.decline_chat_join_request(chat_id, user.id)
            except TelegramAPIError as e:
                logger.error("Error banning user", user_id=user.id, error=str(e))
            join_req.decision = DecisionEnum.banned
            join_req.resolved_at = datetime.now(timezone.utc)
            db.add(ModerationLog(
                group_id=group.id, join_request_id=join_req.id,
                user_id=user.id, username=user.username,
                first_name=user.first_name, last_name=user.last_name,
                decision=DecisionEnum.banned, score=scoring_result.total_score,
                matched_rules=scoring_result.matched_rule_ids,
            ))
            await db.commit()
            await send_decision(
                bot, DecisionEnum.banned, user.id, user_name, user.username,
                scoring_result.total_score, matched_rules_dicts,
                group.title or str(chat_id), group.username,
                reason="score threshold" if scoring_result.total_score <= -100 else None,
            )
            return

        if scoring_result.decision == DecisionEnum.approved:
            try:
                await bot.approve_chat_join_request(chat_id, user.id)
                welcome = await _get_config_value(group.id, "welcome_message", None, db)
                if welcome:
                    await _send_message_safe(bot, user.id, welcome)
            except TelegramAPIError as e:
                logger.error("Error approving user", user_id=user.id, error=str(e))
            join_req.decision = DecisionEnum.approved
            join_req.resolved_at = datetime.now(timezone.utc)
            db.add(ModerationLog(
                group_id=group.id, join_request_id=join_req.id,
                user_id=user.id, username=user.username,
                first_name=user.first_name, last_name=user.last_name,
                decision=DecisionEnum.approved, score=scoring_result.total_score,
                matched_rules=scoring_result.matched_rule_ids,
            ))
            await db.commit()
            await send_decision(
                bot, DecisionEnum.approved, user.id, user_name, user.username,
                scoring_result.total_score, matched_rules_dicts,
                group.title or str(chat_id), group.username,
            )
            return

        if scoring_result.decision == DecisionEnum.manual_review:
            join_req.decision = DecisionEnum.manual_review
            db.add(ModerationLog(
                group_id=group.id, join_request_id=join_req.id,
                user_id=user.id, username=user.username,
                first_name=user.first_name, last_name=user.last_name,
                decision=DecisionEnum.manual_review, score=scoring_result.total_score,
                matched_rules=scoring_result.matched_rule_ids,
            ))
            await db.commit()
            notify_admin = await _get_config_value(
                group.id, "notify_admin_on_manual_review", False, db
            )
            if notify_admin:
                await _notify_manual_review(bot, user, group, join_req, scoring_result)
            await send_decision(
                bot, DecisionEnum.manual_review, user.id, user_name, user.username,
                scoring_result.total_score, matched_rules_dicts,
                group.title or str(chat_id), group.username,
            )
            return

        if scoring_result.requires_verification:
            question = await _VERIFICATION_ENGINE.get_random_question(group.id, db)
            if question:
                await _VERIFICATION_ENGINE.create_session(user.id, chat_id, question, db)
                try:
                    await bot.send_message(
                        user.id,
                        f"🔐 <b>נדרש אימות</b>\n\n{esc(question.question)}\n\n"
                        f"יש לך {question.max_attempts} ניסיון/ות ו-{question.timeout_seconds // 60} דקות לענות.",
                        parse_mode="HTML",
                    )
                except TelegramAPIError as e:
                    logger.error("Could not send verification question", user_id=user.id, error=str(e))
            else:
                # No questions configured - decide by score
                if scoring_result.total_score >= 0:
                    try:
                        await bot.approve_chat_join_request(chat_id, user.id)
                    except TelegramAPIError:
                        pass
                    join_req.decision = DecisionEnum.approved
                else:
                    try:
                        await bot.decline_chat_join_request(chat_id, user.id)
                    except TelegramAPIError:
                        pass
                    join_req.decision = DecisionEnum.rejected
                join_req.resolved_at = datetime.now(timezone.utc)

            db.add(ModerationLog(
                group_id=group.id, join_request_id=join_req.id,
                user_id=user.id, username=user.username,
                first_name=user.first_name, last_name=user.last_name,
                decision=join_req.decision, score=scoring_result.total_score,
                matched_rules=scoring_result.matched_rule_ids,
            ))
            await db.commit()
            return

        # Default: reject
        try:
            await bot.decline_chat_join_request(chat_id, user.id)
        except TelegramAPIError as e:
            logger.error("Error declining user", user_id=user.id, error=str(e))
        join_req.decision = DecisionEnum.rejected
        join_req.resolved_at = datetime.now(timezone.utc)
        db.add(ModerationLog(
            group_id=group.id, join_request_id=join_req.id,
            user_id=user.id, username=user.username,
            first_name=user.first_name, last_name=user.last_name,
            decision=DecisionEnum.rejected, score=scoring_result.total_score,
            matched_rules=scoring_result.matched_rule_ids,
        ))
        await db.commit()
        await send_decision(
            bot, DecisionEnum.rejected, user.id, user_name, user.username,
            scoring_result.total_score, matched_rules_dicts,
            group.title or str(chat_id), group.username,
        )
