"""Background cleanup tasks for expired verification sessions."""
import asyncio
from datetime import datetime, timezone
from aiogram import Bot
from sqlalchemy import select
from database.session import AsyncSessionLocal
from database.models import (
    VerificationSession, JoinRequest, DecisionEnum, ModerationLog, Group, TelegramUser
)
from tracking.channel import send_decision
from logs import get_logger

logger = get_logger(__name__)


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

        if not expired_sessions:
            return

        logger.info("Found expired verification sessions to clean up", count=len(expired_sessions))

        for session in expired_sessions:
            user_id = session.user_id
            chat_id = session.chat_id

            # 1. Complete the verification session as failed due to timeout
            session.completed_at = now
            session.passed = False

            # 2. Find the corresponding pending join request
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

            if join_req:
                join_req.decision = DecisionEnum.rejected
                join_req.resolved_at = now

                # Fetch user details for logging and tracking
                user_stmt = select(TelegramUser).where(TelegramUser.telegram_id == user_id)
                user_result = await db.execute(user_stmt)
                user_record = user_result.scalar_one_or_none()

                username = user_record.username if user_record else None
                first_name = user_record.first_name if user_record else ""
                last_name = user_record.last_name if user_record else ""
                user_name = f"{first_name} {last_name}".strip() or str(user_id)

                # Fetch group details for tracking
                group_stmt = select(Group).where(Group.id == join_req.group_id)
                group_result = await db.execute(group_stmt)
                group = group_result.scalar_one_or_none()

                # Log the moderation decision
                db.add(ModerationLog(
                    group_id=join_req.group_id,
                    join_request_id=join_req.id,
                    user_id=user_id,
                    username=username,
                    first_name=first_name,
                    last_name=last_name,
                    decision=DecisionEnum.rejected,
                    score=join_req.score,
                    matched_rules=join_req.matched_rules,
                    details={"verification_timeout": True},
                ))

                # 3. Decline the chat join request on Telegram (remove but do not ban)
                try:
                    await bot.decline_chat_join_request(chat_id, user_id)
                    logger.info("Declined join request due to verification timeout", user_id=user_id, chat_id=chat_id)
                except Exception as e:
                    logger.warning("Failed to decline join request on Telegram", user_id=user_id, chat_id=chat_id, error=str(e))

                # Send notification to user about the timeout
                try:
                    await bot.send_message(
                        user_id,
                        "⏰ תוקף האימות פג. שלח בקשת הצטרפות חדשה לקבוצה אם ברצונך להתאמת שוב."
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
            else:
                logger.warning("No pending join request found for expired session", user_id=user_id, chat_id=chat_id)

        await db.commit()


async def start_cleanup_loop(bot: Bot, interval_seconds: int = 60) -> None:
    """Runs a background loop that periodically cleans up expired verification sessions."""
    logger.info("Starting background verification cleanup loop...")
    while True:
        try:
            await cleanup_expired_sessions(bot)
        except Exception as e:
            logger.error("Error in verification cleanup loop", error=str(e))
        await asyncio.sleep(interval_seconds)
