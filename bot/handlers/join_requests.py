"""Handles chat_join_request events — multi-tenant moderation flow."""
from datetime import datetime, timezone
from typing import Optional

from aiogram import Router, Bot
from aiogram.types import ChatJoinRequest

from database.session import AsyncSessionLocal
from database.models import (
    DecisionEnum, TelegramUser, JoinRequest, ModerationLog, Group,
    GroupConfig,
)
from moderation import ModerationEngine
from verification import VerificationEngine
from bot.keyboards.settings_kb import admin_review_kb
from tracking import send_decision
from sqlalchemy import select
from logs import get_logger

logger = get_logger(__name__)
router = Router()

_moderation_engine: Optional[ModerationEngine] = None
_verification_engine: Optional[VerificationEngine] = None


def setup_engines(mod_engine: ModerationEngine, ver_engine: VerificationEngine) -> None:
    global _moderation_engine, _verification_engine
    _moderation_engine = mod_engine
    _verification_engine = ver_engine


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
    if not cfg:
        return default
    return cfg.value


async def _send_message_safe(bot: Bot, user_id: int, text: str) -> None:
    try:
        await bot.send_message(user_id, text, parse_mode="HTML")
    except Exception as e:
        logger.warning("Could not send message to user", user_id=user_id, error=str(e))


async def _notify_manual_review(
    bot: Bot,
    user,
    group: Group,
    join_req: JoinRequest,
    scoring_result,
) -> None:
    name = f"{user.first_name or ''} {user.last_name or ''}".strip()
    username_str = f"@{user.username}" if user.username else "no username"
    rules_str = ", ".join(scoring_result.matched_rule_ids) or "—"

    text = (
        f"⚠️ <b>סקירה ידנית נדרשת</b>\n\n"
        f"📍 קבוצה: {group.title}\n"
        f"👤 {name} ({username_str})\n"
        f"🆔 ID: <code>{user.id}</code>\n"
        f"📊 ניקוד: {scoring_result.total_score:+.1f}\n"
        f"🔍 כללים: {rules_str}"
    )
    kb = admin_review_kb(user.id, join_req.id, group.id)

    # Send to group owner
    try:
        await bot.send_message(group.owner_id, text, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
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
            "Join request received during active raid — applying strict scoring",
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
        db_user = await _ensure_user(user, db)

        user_data = {
            "user_id": user.id,
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "username": user.username or "",
        }

        scoring_result = await _moderation_engine.evaluate(
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
            except Exception as e:
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
            except Exception as e:
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
            await _notify_manual_review(bot, user, group, join_req, scoring_result)
            await send_decision(
                bot, DecisionEnum.manual_review, user.id, user_name, user.username,
                scoring_result.total_score, matched_rules_dicts,
                group.title or str(chat_id), group.username,
            )
            return

        if scoring_result.requires_verification:
            question = await _verification_engine.get_random_question(group.id, db)
            if question:
                await _verification_engine.create_session(user.id, chat_id, question, db)
                try:
                    await bot.send_message(
                        user.id,
                        f"🔐 <b>נדרש אימות</b>\n\n{question.question}\n\n"
                        f"יש לך {question.max_attempts} ניסיון/ות ו-{question.timeout_seconds // 60} דקות לענות.",
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error("Could not send verification question", user_id=user.id, error=str(e))
            else:
                # No questions configured — decide by score
                if scoring_result.total_score >= 0:
                    try:
                        await bot.approve_chat_join_request(chat_id, user.id)
                    except Exception:
                        pass
                    join_req.decision = DecisionEnum.approved
                else:
                    try:
                        await bot.decline_chat_join_request(chat_id, user.id)
                    except Exception:
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
        except Exception as e:
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
