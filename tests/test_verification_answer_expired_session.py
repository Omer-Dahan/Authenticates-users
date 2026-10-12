"""Regression test for the expired-session branch of
bot/handlers/verification.py (handle_verification_answer): a backlog answer
that arrives after the session already expired must not close the local
join_request row unless Telegram actually confirms the decline (or that the
request is already gone) - same honest-closure rule as verification/cleanup.py
and moderation/actions.py."""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from database.models import DecisionEnum, JoinRequest, TelegramUser
from bot.handlers.verification import handle_verification_answer
from tests.conftest import make_tg_user


async def _expired_but_answered(db_session, group, user_id):
    now = datetime.now(timezone.utc)
    db_session.add(TelegramUser(telegram_id=user_id, first_name="Dana"))
    jr = JoinRequest(
        group_id=group.id, user_id=user_id, chat_id=group.chat_id,
        decision=DecisionEnum.pending, score=10.0, matched_rules=[],
        created_at=now - timedelta(minutes=20),
    )
    db_session.add(jr)
    await db_session.commit()
    await db_session.refresh(jr)
    return jr


def _answer_message(user_id: int) -> Message:
    msg = AsyncMock(spec=Message)
    msg.from_user = make_tg_user(user_id=user_id)
    msg.text = "42"
    msg.reply = AsyncMock()
    return msg


@pytest.mark.asyncio
async def test_decline_success_closes_the_row(
    test_session_factory, sample_group, db_session, mock_bot, setup_bot_engines
):
    jr = await _expired_but_answered(db_session, sample_group, 60001)

    await handle_verification_answer(_answer_message(60001), mock_bot)

    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_decline_refused_leaves_the_row_open(
    test_session_factory, sample_group, db_session, mock_bot, setup_bot_engines
):
    jr = await _expired_but_answered(db_session, sample_group, 60002)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: some unexpected error"
    )

    await handle_verification_answer(_answer_message(60002), mock_bot)

    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_decline_request_already_gone_still_closes_the_row(
    test_session_factory, sample_group, db_session, mock_bot, setup_bot_engines
):
    jr = await _expired_but_answered(db_session, sample_group, 60003)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: HIDE_REQUESTER_MISSING"
    )

    await handle_verification_answer(_answer_message(60003), mock_bot)

    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None
