"""Handles verification answer messages in private chat — multi-tenant."""
from datetime import datetime, timezone

from aiogram import Router, Bot, F
from aiogram.types import Message
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import (
    JoinRequest, DecisionEnum, TelegramUser, GroupConfig, ModerationLog, Group,
)
from moderation import ModerationEngine
from verification import VerificationEngine
from tracking import send_decision
from logs import get_logger

logger = get_logger(__name__)
router = Router()

_moderation_engine: ModerationEngine | None = None
_verification_engine: VerificationEngine | None = None


def setup_engines(mod_engine: ModerationEngine, ver_engine: VerificationEngine) -> None:
    global _moderation_engine, _verification_engine
    _moderation_engine = mod_engine
    _verification_engine = ver_engine


async def _get_config_msg(group_id: int, key: str, db) -> str | None:
    result = await db.execute(
        select(GroupConfig).where(GroupConfig.group_id == group_id, GroupConfig.key == key)
    )
    cfg = result.scalar_one_or_none()
    return cfg.value if cfg else None


@router.message(F.chat.type == "private")
async def handle_verification_answer(message: Message, bot: Bot) -> None:
    # Skip commands — they're handled by other handlers
    if message.text and message.text.startswith("/"):
        return

    user_id = message.from_user.id
    text = message.text or ""

    async with AsyncSessionLocal() as db:
        jr_result = await db.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user_id,
                JoinRequest.decision == DecisionEnum.pending,
            ).order_by(JoinRequest.created_at.desc())
        )
        join_req = jr_result.scalars().first()

        if not join_req:
            return

        chat_id = join_req.chat_id
        group_id = join_req.group_id

        session = await _verification_engine.get_active_session(user_id, chat_id, db)

        if not session:
            await message.reply("⏰ תוקף האימות פג. שלח בקשת הצטרפות חדשה.")
            try:
                await bot.decline_chat_join_request(chat_id, user_id)
            except Exception:
                pass
            join_req.decision = DecisionEnum.rejected
            join_req.resolved_at = datetime.now(timezone.utc)
            await db.commit()
            return

        passed, reply_text, verification_score = await _verification_engine.submit_answer(
            session, text, db
        )
        await message.reply(reply_text)

        if verification_score is None:
            await db.commit()
            return

        # Verification concluded — re-evaluate with score
        user_result = await db.get(TelegramUser, user_id)
        user_data = {
            "user_id": user_id,
            "first_name": (user_result.first_name or "") if user_result else "",
            "last_name": (user_result.last_name or "") if user_result else "",
            "username": (user_result.username or "") if user_result else "",
        }

        final_result = await _moderation_engine.evaluate(
            user_data, db, group_id=group_id, verification_score=verification_score
        )

        # Load group for tracking
        group = await db.get(Group, group_id) if group_id else None

        ban_user = final_result.decision == DecisionEnum.banned or (
            not passed and await _verification_engine.should_ban(session, db)
        )

        if ban_user:
            try:
                await bot.ban_chat_member(chat_id, user_id)
                await bot.decline_chat_join_request(chat_id, user_id)
            except Exception as e:
                logger.error("Error banning after failed verification", error=str(e))
            join_req.decision = DecisionEnum.banned

        elif final_result.decision == DecisionEnum.approved:
            try:
                await bot.approve_chat_join_request(chat_id, user_id)
                approve_msg = await _get_config_msg(group_id, "approve_message", db) if group_id else None
                if approve_msg:
                    try:
                        await bot.send_message(user_id, approve_msg)
                    except Exception:
                        pass
            except Exception as e:
                logger.error("Error approving after verification", error=str(e))
            join_req.decision = DecisionEnum.approved

        else:
            try:
                await bot.decline_chat_join_request(chat_id, user_id)
                reject_msg = await _get_config_msg(group_id, "reject_message", db) if group_id else None
                if reject_msg:
                    try:
                        await bot.send_message(user_id, reject_msg)
                    except Exception:
                        pass
            except Exception as e:
                logger.error("Error declining after verification", error=str(e))
            join_req.decision = DecisionEnum.rejected

        join_req.score = final_result.total_score
        join_req.matched_rules = final_result.matched_rule_ids
        join_req.resolved_at = datetime.now(timezone.utc)

        db.add(ModerationLog(
            group_id=group_id,
            join_request_id=join_req.id,
            user_id=user_id,
            username=user_result.username if user_result else None,
            first_name=user_result.first_name if user_result else None,
            last_name=user_result.last_name if user_result else None,
            decision=join_req.decision,
            score=join_req.score,
            matched_rules=join_req.matched_rules,
            details={"verification": True, "verification_score": verification_score},
        ))
        await db.commit()

        logger.info("Verification flow completed", user_id=user_id, decision=join_req.decision.value)

        # Tracking channel
        if group:
            user_name = f"{user_result.first_name or ''} {user_result.last_name or ''}".strip() if user_result else str(user_id)
            matched_dicts = [
                {"rule_id": r.rule_id, "rule_name": r.rule_name, "score": r.score}
                for r in final_result.matched_rules
            ]
            await send_decision(
                bot, join_req.decision, user_id, user_name,
                user_result.username if user_result else None,
                final_result.total_score, matched_dicts,
                group.title or str(chat_id), group.username,
            )
