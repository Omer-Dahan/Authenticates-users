"""Per-group verification question/answer engine."""
import re
import random
from datetime import datetime, timezone, timedelta
from typing import Optional, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database.models import GroupQuestion, VerificationSession, TelegramUser
from logs import get_logger

logger = get_logger(__name__)


def _validate_answer(given: str, question: GroupQuestion) -> bool:
    accepted = question.accepted_answers or []
    val_type = question.validation_type or "exact_match"

    if not question.case_sensitive:
        given = given.strip().lower()
        accepted = [a.lower() for a in accepted]
    else:
        given = given.strip()

    if val_type == "exact_match":
        return given in accepted

    if val_type == "keyword":
        return any(kw in given for kw in accepted)

    if val_type == "regex":
        for pattern in accepted:
            try:
                flags = 0 if question.case_sensitive else re.IGNORECASE
                if re.search(pattern, given, flags):
                    return True
            except re.error:
                logger.warning("Invalid regex in verification answer", pattern=pattern)
        return False

    return given in accepted


class VerificationEngine:
    async def get_random_question(
        self,
        group_id: int,
        db: AsyncSession,
    ) -> Optional[GroupQuestion]:
        result = await db.execute(
            select(GroupQuestion).where(
                GroupQuestion.group_id == group_id,
                GroupQuestion.enabled == True,
            )
        )
        questions = result.scalars().all()
        if not questions:
            return None
        return random.choice(questions)

    async def create_session(
        self,
        user_id: int,
        chat_id: int,
        question: GroupQuestion,
        db: AsyncSession,
    ) -> VerificationSession:
        existing = await db.execute(
            select(VerificationSession).where(
                VerificationSession.user_id == user_id,
                VerificationSession.chat_id == chat_id,
                VerificationSession.completed_at.is_(None),
            )
        )
        for old in existing.scalars().all():
            old.completed_at = datetime.now(timezone.utc)

        expires_at = datetime.now(timezone.utc) + timedelta(seconds=question.timeout_seconds)
        session = VerificationSession(
            user_id=user_id,
            chat_id=chat_id,
            question_id=question.id,
            attempts=0,
            expires_at=expires_at,
        )
        db.add(session)
        await db.flush()
        return session

    async def get_active_session(
        self,
        user_id: int,
        chat_id: int,
        db: AsyncSession,
    ) -> Optional[VerificationSession]:
        result = await db.execute(
            select(VerificationSession).where(
                VerificationSession.user_id == user_id,
                VerificationSession.chat_id == chat_id,
                VerificationSession.completed_at.is_(None),
            )
        )
        session = result.scalar_one_or_none()

        if session and session.expires_at:
            expires = session.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) > expires:
                session.completed_at = datetime.now(timezone.utc)
                session.passed = False
                await db.flush()
                return None

        return session

    async def submit_answer(
        self,
        session: VerificationSession,
        answer: str,
        db: AsyncSession,
    ) -> Tuple[bool, str, Optional[float]]:
        """Returns (passed, message, score_or_None)."""
        question = await db.get(GroupQuestion, session.question_id)
        if not question:
            return False, "❌ שאלה לא נמצאה.", None

        session.attempts += 1
        session.answer_given = answer
        correct = _validate_answer(answer, question)

        if correct:
            session.passed = True
            session.completed_at = datetime.now(timezone.utc)
            await db.flush()
            return True, "✅ נכון! מעבד את הבקשה...", question.score_on_pass

        remaining = question.max_attempts - session.attempts
        if remaining <= 0:
            session.passed = False
            session.completed_at = datetime.now(timezone.utc)
            await db.flush()
            return False, "❌ המקסימום ניסיונות הושג.", question.score_on_fail

        await db.flush()
        return False, f"❌ תשובה שגויה. {remaining} ניסיון/ות נותר/ים.", None

    async def should_ban(self, session: VerificationSession, db: AsyncSession) -> bool:
        question = await db.get(GroupQuestion, session.question_id)
        return bool(question and question.ban_on_fail and session.passed is False)
