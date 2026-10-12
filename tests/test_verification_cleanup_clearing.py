"""Regression tests for the honest clearing of expired verification sessions
(verification/cleanup.py, cleanup_expired_sessions). Reproduces the production
bug: a refused decline was treated as success and the local row was closed
anyway, even though Telegram still held the request. See
docs/DELETED-REQUEST-REMOVAL.md for the live evidence."""
from datetime import datetime, timedelta, timezone

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import ChatMemberLeft, ChatMemberMember, User

from database.models import DecisionEnum, JoinRequest, ModerationLog, TelegramUser, UserBan, VerificationSession
import verification.cleanup
from verification.cleanup import cleanup_expired_sessions
from sqlalchemy import select


def _deleted_member() -> ChatMemberMember:
    user = User(id=0, is_bot=False, first_name="", username=None, last_name=None)
    return ChatMemberMember(status="member", user=user)


def _left_named(user_id: int) -> ChatMemberLeft:
    return ChatMemberLeft(status="left", user=User(id=user_id, is_bot=False, first_name="Dana"))


async def _expired_session(db_session, group, user_id):
    now = datetime.now(timezone.utc)
    db_session.add(TelegramUser(telegram_id=user_id, first_name="Dana"))
    jr = JoinRequest(
        group_id=group.id, user_id=user_id, chat_id=group.chat_id,
        decision=DecisionEnum.pending, score=10.0, matched_rules=[],
    )
    db_session.add(jr)
    db_session.add(VerificationSession(
        user_id=user_id, chat_id=group.chat_id,
        attempts=0, notified_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(minutes=1),
    ))
    await db_session.commit()
    await db_session.refresh(jr)
    return jr


@pytest.mark.asyncio
async def test_decline_refused_for_deactivated_account_is_cleared_via_ban(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _expired_session(db_session, sample_group, 50001)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )
    mock_bot.get_chat_member.return_value = _deleted_member()

    await cleanup_expired_sessions(mock_bot)

    mock_bot.ban_chat_member.assert_called_once_with(sample_group.chat_id, 50001)
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.cleared_via_ban
        assert row.resolved_at is not None

        logs = (await check_db.execute(
            select(ModerationLog).where(ModerationLog.join_request_id == jr.id)
        )).scalars().all()
        assert len(logs) == 1
        assert logs[0].details.get("cleared_via") == "ban"

        session = (await check_db.execute(
            select(VerificationSession).where(VerificationSession.user_id == 50001)
        )).scalars().first()
        assert session.completed_at is not None

        # The ban must be recorded the same way every other ban access path
        # records one, so /unban can find it and a future admin can see what
        # happened - not an invisible, unrecorded Telegram-only side effect.
        ban_row = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == 50001, UserBan.lifted_at.is_(None))
        )).scalars().first()
        assert ban_row is not None
        assert ban_row.scope == "local"
        assert ban_row.group_id == sample_group.id


@pytest.mark.asyncio
async def test_decline_refused_for_a_live_account_never_escalates_to_ban_and_stays_open(
    test_session_factory, sample_group, db_session, mock_bot
):
    """The owner's hard rule: never ban a live applicant. If decline fails
    for a live, resolvable account, the bot must not guess and must not
    ban - the row stays open for a human or a later sweep."""
    jr = await _expired_session(db_session, sample_group, 50002)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: some unexpected error"
    )
    mock_bot.get_chat_member.return_value = _left_named(50002)

    await cleanup_expired_sessions(mock_bot)

    mock_bot.ban_chat_member.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None

        # The verification session itself must also stay open (not marked
        # completed before the Telegram attempt), or the next loop iteration
        # would never find it again to retry the clearing attempt.
        session = (await check_db.execute(
            select(VerificationSession).where(VerificationSession.user_id == 50002)
        )).scalars().first()
        assert session.completed_at is None


@pytest.mark.asyncio
async def test_a_failed_attempt_is_actually_retried_on_the_next_loop_run(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Reproduces finding G directly: the comment used to claim a failed
    attempt was "left open for a later retry" while the session was already
    marked completed before the Telegram call ever ran, so the next loop
    iteration could never find it again. Here the first run's decline fails,
    then - once the backoff window has elapsed - the second run's decline
    succeeds, proving the retry actually happens, not just that the row
    looks open."""
    jr = await _expired_session(db_session, sample_group, 50006)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: some unexpected error"
    )
    mock_bot.get_chat_member.return_value = _left_named(50006)

    await cleanup_expired_sessions(mock_bot)

    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None
        session = (await check_db.execute(
            select(VerificationSession).where(VerificationSession.user_id == 50006)
        )).scalars().first()
        assert session.completed_at is None

    # A failed attempt is backed off, not retried hot on the very next tick
    # (see verification.cleanup._CLEAR_RETRY_STATE) - push the recorded
    # last-attempt time far enough into the past to simulate the backoff
    # window having elapsed, exactly like a later real loop iteration.
    failures, _ = verification.cleanup._CLEAR_RETRY_STATE[session.id]
    verification.cleanup._CLEAR_RETRY_STATE[session.id] = (
        failures, datetime.now(timezone.utc) - timedelta(hours=2),
    )

    mock_bot.decline_chat_join_request.side_effect = None
    mock_bot.decline_chat_join_request.return_value = True

    await cleanup_expired_sessions(mock_bot)

    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None
        session = (await check_db.execute(
            select(VerificationSession).where(VerificationSession.user_id == 50006)
        )).scalars().first()
        assert session.completed_at is not None


@pytest.mark.asyncio
async def test_failed_attempt_is_not_retried_hot_on_the_very_next_tick(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Finding 1: a row that keeps failing to clear must not be retried on
    every single 60-second loop tick forever - it is backed off instead.
    Two cleanup runs back to back (no time elapsed between them, exactly
    like two consecutive loop ticks) must only hit Telegram once."""
    await _expired_session(db_session, sample_group, 50007)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: some unexpected error"
    )
    mock_bot.get_chat_member.return_value = _left_named(50007)

    await cleanup_expired_sessions(mock_bot)
    assert mock_bot.decline_chat_join_request.call_count == 1

    await cleanup_expired_sessions(mock_bot)
    assert mock_bot.decline_chat_join_request.call_count == 1

    async with test_session_factory() as check_db:
        row = (await check_db.execute(
            select(JoinRequest).where(JoinRequest.user_id == 50007)
        )).scalars().first()
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_decline_refused_for_an_access_reason_never_escalates_and_stays_open(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _expired_session(db_session, sample_group, 50003)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: chat not found"
    )

    await cleanup_expired_sessions(mock_bot)

    mock_bot.get_chat_member.assert_not_called()
    mock_bot.ban_chat_member.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_decline_refused_and_ban_also_refused_leaves_row_open(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _expired_session(db_session, sample_group, 50004)
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.ban_chat_member.side_effect = TelegramAPIError(
        method="banChatMember", message="Forbidden: user is deactivated"
    )

    await cleanup_expired_sessions(mock_bot)

    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_plain_decline_success_is_unaffected_by_the_escalation_path(
    test_session_factory, sample_group, db_session, mock_bot
):
    """The common case (a live applicant whose decline simply succeeds) must
    keep working exactly as before, with no probe and no ban attempted."""
    jr = await _expired_session(db_session, sample_group, 50005)

    await cleanup_expired_sessions(mock_bot)

    mock_bot.get_chat_member.assert_not_called()
    mock_bot.ban_chat_member.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None
