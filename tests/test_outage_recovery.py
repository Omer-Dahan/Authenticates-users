"""Regression tests for automatic downtime / outage recovery (docs/RECOVERY.md)."""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from aiogram.types import ChatMemberUpdated, ChatMemberLeft, ChatMemberMember

import bot.handlers.member_events as member_events
from database.models import (
    JoinRequest, DecisionEnum, VerificationSession, GroupQuestion,
    BotState, OutageLog, MemberEvent,
)
from recovery.state import detect_outage
from recovery.sweep import run_recovery_sweep
from verification.cleanup import cleanup_expired_sessions
from verification import VerificationEngine
from tests.conftest import make_tg_user, make_tg_chat


def _aware(dt):
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@pytest.mark.asyncio
async def test_fresh_db_seeds_state_without_recovery(test_session_factory, db_session):
    outage_started_at, outage_seconds = await detect_outage(db_session)

    assert outage_started_at is None
    assert outage_seconds == 0.0

    async with test_session_factory() as check_session:
        state = await check_session.get(BotState, 1)
        assert state is not None
        assert state.last_heartbeat_at is not None


@pytest.mark.asyncio
async def test_sends_question_to_unresolved_join_requests_after_outage(
    test_session_factory, sample_group, mock_bot, db_session
):
    db_session.add(GroupQuestion(
        group_id=sample_group.id,
        question="מהי בירת ישראל?",
        accepted_answers=["ירושלים"],
        validation_type="exact_match",
        max_attempts=2,
        timeout_seconds=300,
        enabled=True,
    ))
    user_ids = [40011, 40012, 40013]
    for uid in user_ids:
        db_session.add(JoinRequest(
            group_id=sample_group.id, user_id=uid, chat_id=sample_group.chat_id,
            decision=DecisionEnum.pending, score=10.0,
        ))
    await db_session.commit()

    mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))

    now = datetime.now(timezone.utc)
    outage_started_at = now - timedelta(minutes=10)
    outage_seconds = 600.0

    ver_engine = VerificationEngine()
    event = asyncio.Event()
    await run_recovery_sweep(mock_bot, ver_engine, outage_started_at, outage_seconds, 0, event)

    assert event.is_set()

    question_dms = [
        c for c in mock_bot.send_message.call_args_list
        if c.args and c.args[0] in user_ids and "נדרש אימות" in c.args[1]
    ]
    assert len(question_dms) == 3

    async with test_session_factory() as check_session:
        for uid in user_ids:
            res = await check_session.execute(
                select(VerificationSession).where(VerificationSession.user_id == uid)
            )
            sessions = res.scalars().all()
            assert len(sessions) == 1
            session = sessions[0]
            assert session.notified_at is not None
            expected_expiry = now + timedelta(seconds=300)
            assert abs((_aware(session.expires_at) - expected_expiry).total_seconds()) < 5


@pytest.mark.asyncio
async def test_extends_session_that_expired_during_outage(
    test_session_factory, sample_group, mock_bot, db_session
):
    user_id = 40021
    now = datetime.now(timezone.utc)
    outage_started_at = now - timedelta(minutes=10)
    outage_seconds = 600.0

    db_session.add(JoinRequest(
        group_id=sample_group.id, user_id=user_id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.pending, score=50.0,
    ))
    old_expires_at = now - timedelta(minutes=2)
    session = VerificationSession(
        user_id=user_id, chat_id=sample_group.chat_id,
        attempts=0, notified_at=now - timedelta(minutes=15),
        expires_at=old_expires_at,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)

    mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))

    ver_engine = VerificationEngine()
    await run_recovery_sweep(mock_bot, ver_engine, outage_started_at, outage_seconds, 0, asyncio.Event())

    async with test_session_factory() as check_session:
        updated = await check_session.get(VerificationSession, session.id)
        expected = old_expires_at + timedelta(seconds=outage_seconds)
        assert abs((_aware(updated.expires_at) - expected).total_seconds()) < 2
        assert updated.completed_at is None

    notice_calls = [
        c for c in mock_bot.send_message.call_args_list
        if c.args and c.args[0] == user_id
    ]
    assert len(notice_calls) == 1
    assert "הוארך" in notice_calls[0].args[1]

    # The now-extended (future) session must not be declined by the cleanup loop
    await cleanup_expired_sessions(mock_bot)
    mock_bot.decline_chat_join_request.assert_not_called()


@pytest.mark.asyncio
async def test_sweep_is_idempotent_when_run_twice(
    test_session_factory, sample_group, mock_bot, db_session
):
    db_session.add(GroupQuestion(
        group_id=sample_group.id,
        question="מהי בירת ישראל?",
        accepted_answers=["ירושלים"],
        validation_type="exact_match",
        max_attempts=2,
        timeout_seconds=300,
        enabled=True,
    ))
    user_ids = [40031, 40032, 40033]
    for uid in user_ids:
        db_session.add(JoinRequest(
            group_id=sample_group.id, user_id=uid, chat_id=sample_group.chat_id,
            decision=DecisionEnum.pending, score=10.0,
        ))
    await db_session.commit()

    mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="left"))

    outage_started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    ver_engine = VerificationEngine()

    await run_recovery_sweep(mock_bot, ver_engine, outage_started_at, 600.0, 0, asyncio.Event())
    await run_recovery_sweep(mock_bot, ver_engine, outage_started_at, 600.0, 0, asyncio.Event())

    question_dms = [
        c for c in mock_bot.send_message.call_args_list
        if c.args and c.args[0] in user_ids and "נדרש אימות" in c.args[1]
    ]
    assert len(question_dms) == 3

    async with test_session_factory() as check_session:
        for uid in user_ids:
            res = await check_session.execute(
                select(VerificationSession).where(VerificationSession.user_id == uid)
            )
            assert len(res.scalars().all()) == 1


@pytest.mark.asyncio
async def test_already_member_is_approved_without_dm(
    test_session_factory, sample_group, mock_bot, db_session
):
    user_id = 40041
    jr = JoinRequest(
        group_id=sample_group.id, user_id=user_id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.pending, score=10.0,
    )
    db_session.add(jr)
    await db_session.commit()
    await db_session.refresh(jr)

    mock_bot.get_chat_member = AsyncMock(return_value=MagicMock(status="member"))

    ver_engine = VerificationEngine()
    outage_started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await run_recovery_sweep(mock_bot, ver_engine, outage_started_at, 600.0, 0, asyncio.Event())

    async with test_session_factory() as check_session:
        updated = await check_session.get(JoinRequest, jr.id)
        assert updated.decision == DecisionEnum.approved
        assert updated.resolved_at is not None

    dm_calls = [c for c in mock_bot.send_message.call_args_list if c.args and c.args[0] == user_id]
    assert len(dm_calls) == 0


@pytest.mark.asyncio
async def test_cleanup_loop_skips_session_in_unreconciled_outage(
    test_session_factory, sample_group, mock_bot, db_session
):
    now = datetime.now(timezone.utc)
    outage_started_at = now - timedelta(minutes=20)

    db_session.add(OutageLog(
        started_at=outage_started_at, ended_at=None, duration_seconds=1200.0,
    ))

    user_id = 40051
    jr = JoinRequest(
        group_id=sample_group.id, user_id=user_id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.pending, score=10.0,
    )
    db_session.add(jr)

    session = VerificationSession(
        user_id=user_id, chat_id=sample_group.chat_id,
        attempts=0, notified_at=now - timedelta(minutes=25),
        expires_at=now - timedelta(minutes=10),
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    await db_session.refresh(jr)

    await cleanup_expired_sessions(mock_bot)

    mock_bot.decline_chat_join_request.assert_not_called()

    async with test_session_factory() as check_session:
        updated_session = await check_session.get(VerificationSession, session.id)
        assert updated_session.completed_at is None

        updated_jr = await check_session.get(JoinRequest, jr.id)
        assert updated_jr.decision == DecisionEnum.pending


@pytest.mark.asyncio
async def test_replayed_chat_member_join_finds_outage_row_before_sweep_runs(
    test_session_factory, sample_group, mock_bot, db_session
):
    """Regression: detect_outage() must open the OutageLog row itself, since
    dp.start_polling() starts replaying queued updates within seconds of
    startup - long before run_recovery_sweep()'s grace-period sleep elapses.
    If the row only appeared once _sweep() ran, this replayed chat_member
    event would find no open outage and the user would never get caught up."""
    db_session.add(GroupQuestion(
        group_id=sample_group.id,
        question="מהי בירת ישראל?",
        accepted_answers=["ירושלים"],
        validation_type="exact_match",
        max_attempts=2,
        timeout_seconds=300,
        enabled=True,
    ))
    past_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.add(BotState(id=1, last_heartbeat_at=past_heartbeat, last_started_at=past_heartbeat))
    await db_session.commit()

    outage_started_at, outage_seconds = await detect_outage(db_session)
    assert outage_started_at is not None

    async with test_session_factory() as check_session:
        open_logs = (await check_session.execute(
            select(OutageLog).where(OutageLog.ended_at.is_(None))
        )).scalars().all()
        assert len(open_logs) == 1

    ver_engine = VerificationEngine()
    member_events.setup_engine(ver_engine)

    user = make_tg_user(user_id=40061)
    chat = make_tg_chat(chat_id=sample_group.chat_id)
    event_at = outage_started_at + timedelta(minutes=1)
    update = ChatMemberUpdated(
        chat=chat,
        from_user=user,
        date=event_at,
        old_chat_member=ChatMemberLeft(status="left", user=user),
        new_chat_member=ChatMemberMember(status="member", user=user),
    )

    await member_events.handle_chat_member_update(update, mock_bot)

    question_dms = [
        c for c in mock_bot.send_message.call_args_list
        if c.args and c.args[0] == user.id and "נדרש אימות" in c.args[1]
    ]
    assert len(question_dms) == 1

    async with test_session_factory() as check_session:
        sessions = (await check_session.execute(
            select(VerificationSession).where(VerificationSession.user_id == user.id)
        )).scalars().all()
        assert len(sessions) == 1
        assert sessions[0].notified_at is not None

        member_event_rows = (await check_session.execute(
            select(MemberEvent).where(MemberEvent.user_id == user.id)
        )).scalars().all()
        assert len(member_event_rows) == 1
        assert member_event_rows[0].during_outage is True


@pytest.mark.asyncio
async def test_catch_up_join_ignores_old_rejected_join_request(
    test_session_factory, sample_group, mock_bot, db_session
):
    """Regression: a join request rejected months ago must not block catch-up
    for a user who re-requested and actually joined during an outage."""
    db_session.add(GroupQuestion(
        group_id=sample_group.id,
        question="מהי בירת ישראל?",
        accepted_answers=["ירושלים"],
        validation_type="exact_match",
        max_attempts=2,
        timeout_seconds=300,
        enabled=True,
    ))
    user_id = 40081
    old_now = datetime.now(timezone.utc) - timedelta(days=90)
    db_session.add(JoinRequest(
        group_id=sample_group.id, user_id=user_id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.rejected, score=10.0,
        resolved_at=old_now,
    ))
    past_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=10)
    db_session.add(BotState(id=1, last_heartbeat_at=past_heartbeat, last_started_at=past_heartbeat))
    await db_session.commit()

    outage_started_at, outage_seconds = await detect_outage(db_session)
    assert outage_started_at is not None

    ver_engine = VerificationEngine()
    member_events.setup_engine(ver_engine)

    user = make_tg_user(user_id=user_id)
    chat = make_tg_chat(chat_id=sample_group.chat_id)
    event_at = outage_started_at + timedelta(minutes=1)
    update = ChatMemberUpdated(
        chat=chat,
        from_user=user,
        date=event_at,
        old_chat_member=ChatMemberLeft(status="left", user=user),
        new_chat_member=ChatMemberMember(status="member", user=user),
    )

    await member_events.handle_chat_member_update(update, mock_bot)

    question_dms = [
        c for c in mock_bot.send_message.call_args_list
        if c.args and c.args[0] == user_id and "נדרש אימות" in c.args[1]
    ]
    assert len(question_dms) == 1

    async with test_session_factory() as check_session:
        sessions = (await check_session.execute(
            select(VerificationSession).where(VerificationSession.user_id == user_id)
        )).scalars().all()
        assert len(sessions) == 1
        assert sessions[0].notified_at is not None


@pytest.mark.asyncio
async def test_sweep_closes_outage_row_when_it_raises_mid_run(
    test_session_factory, sample_group, mock_bot, db_session
):
    """Regression: an OutageLog row left open by a crashed sweep must not
    permanently disable verification/cleanup.py's expired-session handling."""
    outage_started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

    db_session.add(BotState(
        id=1, last_heartbeat_at=outage_started_at, last_started_at=outage_started_at,
        recovery_completed_at=None,
    ))
    db_session.add(JoinRequest(
        group_id=sample_group.id, user_id=40071, chat_id=sample_group.chat_id,
        decision=DecisionEnum.pending, score=10.0,
    ))
    await db_session.commit()

    mock_bot.get_chat_member = AsyncMock(side_effect=RuntimeError("group loop exploded"))

    ver_engine = VerificationEngine()
    event = asyncio.Event()
    await run_recovery_sweep(mock_bot, ver_engine, outage_started_at, 600.0, 0, event)

    assert event.is_set()

    async with test_session_factory() as check_session:
        logs = (await check_session.execute(select(OutageLog))).scalars().all()
        assert len(logs) == 1
        assert logs[0].ended_at is not None

        state = await check_session.get(BotState, 1)
        assert state is not None
        assert state.recovery_completed_at is not None

    # A fresh expired session must still be declined afterwards - the crashed
    # sweep's now-closed row must not have permanently disabled cleanup.
    other_user_id = 40072
    now = datetime.now(timezone.utc)
    async with test_session_factory() as setup_session:
        setup_session.add(JoinRequest(
            group_id=sample_group.id, user_id=other_user_id, chat_id=sample_group.chat_id,
            decision=DecisionEnum.pending, score=10.0,
        ))
        setup_session.add(VerificationSession(
            user_id=other_user_id, chat_id=sample_group.chat_id,
            attempts=0, notified_at=now - timedelta(minutes=20),
            expires_at=now - timedelta(minutes=5),
        ))
        await setup_session.commit()

    await cleanup_expired_sessions(mock_bot)

    decline_calls = [
        c for c in mock_bot.decline_chat_join_request.call_args_list
        if c.args and c.args[1] == other_user_id
    ]
    assert len(decline_calls) == 1
