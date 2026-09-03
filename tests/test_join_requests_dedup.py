import pytest
from datetime import datetime, timezone
from sqlalchemy import select, func
from database.models import Group, GroupConfig, JoinRequest, DecisionEnum, ModerationLog
from bot.handlers.join_requests import handle_join_request
from bot.handlers.settings_menu import cb_manual_review
from tests.conftest import make_join_request, make_tg_user, make_tg_chat, make_callback_query


@pytest.fixture
async def lockdown_notify_group(sample_group, db_session):
    """Configures sample_group in lockdown mode with admin notifications enabled."""
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="security_mode",
        value="lockdown",
        value_type="string",
    ))
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="notify_admin_on_manual_review",
        value="true",
        value_type="bool",
    ))
    await db_session.commit()
    return sample_group


@pytest.mark.asyncio
async def test_duplicate_join_request_manual_review_ignored(
    test_session_factory, lockdown_notify_group, mock_bot, setup_bot_engines
):
    user = make_tg_user(user_id=20001, first_name="DupeUser", username="dupe_user")
    req1 = make_join_request(user=user)

    # First request: should be processed and notify admin
    await handle_join_request(req1, mock_bot, group_db=lockdown_notify_group)

    admin_notifications = [
        call for call in mock_bot.send_message.call_args_list
        if call.args and call.args[0] == lockdown_notify_group.owner_id
    ]
    assert len(admin_notifications) == 1

    # Verify first request in DB
    async with test_session_factory() as session:
        count = await session.scalar(
            select(func.count(JoinRequest.id)).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == lockdown_notify_group.chat_id,
            )
        )
        assert count == 1

        logs_count = await session.scalar(
            select(func.count(ModerationLog.id)).where(ModerationLog.user_id == user.id)
        )
        assert logs_count == 1

    # Second request from same user to same chat while first is unresolved (manual_review)
    req2 = make_join_request(user=user)
    await handle_join_request(req2, mock_bot, group_db=lockdown_notify_group)

    # Admin should NOT receive a second notification
    admin_notifications_after = [
        call for call in mock_bot.send_message.call_args_list
        if call.args and call.args[0] == lockdown_notify_group.owner_id
    ]
    assert len(admin_notifications_after) == 1

    # No second JoinRequest or ModerationLog created
    async with test_session_factory() as session:
        count_after = await session.scalar(
            select(func.count(JoinRequest.id)).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == lockdown_notify_group.chat_id,
            )
        )
        assert count_after == 1

        logs_count_after = await session.scalar(
            select(func.count(ModerationLog.id)).where(ModerationLog.user_id == user.id)
        )
        assert logs_count_after == 1


@pytest.mark.asyncio
async def test_duplicate_join_request_pending_verification_ignored(
    test_session_factory, sample_group, mock_bot, setup_bot_engines, db_session
):
    # Pre-insert an unresolved pending JoinRequest for this user
    user = make_tg_user(user_id=20002, first_name="PendingUser")
    db_session.add(JoinRequest(
        group_id=sample_group.id,
        user_id=user.id,
        chat_id=sample_group.chat_id,
        decision=DecisionEnum.pending,
        score=20.0,
    ))
    await db_session.commit()

    req = make_join_request(user=user)
    await handle_join_request(req, mock_bot, group_db=sample_group)

    # Should not process or create new records
    async with test_session_factory() as session:
        count = await session.scalar(
            select(func.count(JoinRequest.id)).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == sample_group.chat_id,
            )
        )
        assert count == 1


@pytest.mark.asyncio
async def test_different_user_same_chat_is_processed(
    test_session_factory, lockdown_notify_group, mock_bot, setup_bot_engines
):
    user_a = make_tg_user(user_id=20003, first_name="UserA")
    user_b = make_tg_user(user_id=20004, first_name="UserB")

    req_a = make_join_request(user=user_a)
    await handle_join_request(req_a, mock_bot, group_db=lockdown_notify_group)

    req_b = make_join_request(user=user_b)
    await handle_join_request(req_b, mock_bot, group_db=lockdown_notify_group)

    # Both users should have their own JoinRequest
    async with test_session_factory() as session:
        reqs = (await session.execute(
            select(JoinRequest).where(JoinRequest.group_id == lockdown_notify_group.id)
        )).scalars().all()
        user_ids = {r.user_id for r in reqs}
        assert user_a.id in user_ids
        assert user_b.id in user_ids

    # Admin notified twice (once for each distinct user)
    admin_calls = [
        call for call in mock_bot.send_message.call_args_list
        if call.args and call.args[0] == lockdown_notify_group.owner_id
    ]
    assert len(admin_calls) == 2


@pytest.mark.asyncio
async def test_same_user_different_chat_is_processed(
    test_session_factory, lockdown_notify_group, mock_bot, setup_bot_engines, db_session
):
    # Create second group
    second_group = Group(
        chat_id=-1009998887776,
        title="Second Group",
        owner_id=lockdown_notify_group.owner_id,
        is_active=True,
    )
    db_session.add(second_group)
    await db_session.flush()

    db_session.add(GroupConfig(
        group_id=second_group.id,
        key="security_mode",
        value="lockdown",
        value_type="string",
    ))
    db_session.add(GroupConfig(
        group_id=second_group.id,
        key="notify_admin_on_manual_review",
        value="true",
        value_type="bool",
    ))
    await db_session.commit()
    await db_session.refresh(second_group)

    user = make_tg_user(user_id=20005, first_name="MultiGroupUser")

    # Request to Group 1
    req1 = make_join_request(user=user, chat=make_tg_chat(chat_id=lockdown_notify_group.chat_id))
    await handle_join_request(req1, mock_bot, group_db=lockdown_notify_group)

    # Request to Group 2 from same user
    req2 = make_join_request(user=user, chat=make_tg_chat(chat_id=second_group.chat_id))
    await handle_join_request(req2, mock_bot, group_db=second_group)

    # Both requests processed and recorded
    async with test_session_factory() as session:
        res1 = await session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == lockdown_notify_group.chat_id,
            )
        )
        assert res1.scalar_one_or_none() is not None

        res2 = await session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == second_group.chat_id,
            )
        )
        assert res2.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_previously_approved_request_allows_new_request(
    test_session_factory, lockdown_notify_group, mock_bot, setup_bot_engines, db_session
):
    user = make_tg_user(user_id=20006, first_name="RejoinUser")

    # Seed an earlier request that was approved
    old_req = JoinRequest(
        group_id=lockdown_notify_group.id,
        user_id=user.id,
        chat_id=lockdown_notify_group.chat_id,
        decision=DecisionEnum.approved,
        score=100.0,
        resolved_at=datetime.now(timezone.utc),
    )
    db_session.add(old_req)
    await db_session.commit()

    # User re-requests (e.g. rejoined after leaving)
    new_req = make_join_request(user=user)
    await handle_join_request(new_req, mock_bot, group_db=lockdown_notify_group)

    # The new request was NOT blocked, so a new JoinRequest was added
    async with test_session_factory() as session:
        reqs = (await session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == lockdown_notify_group.chat_id,
            ).order_by(JoinRequest.id.asc())
        )).scalars().all()
        assert len(reqs) == 2
        assert reqs[0].decision == DecisionEnum.approved
        assert reqs[1].decision == DecisionEnum.manual_review


@pytest.mark.asyncio
async def test_previously_rejected_request_allows_new_request(
    test_session_factory, lockdown_notify_group, mock_bot, setup_bot_engines, db_session
):
    user = make_tg_user(user_id=20007, first_name="RejectedThenRetryUser")

    # Seed an earlier request that was rejected
    old_req = JoinRequest(
        group_id=lockdown_notify_group.id,
        user_id=user.id,
        chat_id=lockdown_notify_group.chat_id,
        decision=DecisionEnum.rejected,
        score=-50.0,
        resolved_at=datetime.now(timezone.utc),
    )
    db_session.add(old_req)
    await db_session.commit()

    new_req = make_join_request(user=user)
    await handle_join_request(new_req, mock_bot, group_db=lockdown_notify_group)

    async with test_session_factory() as session:
        reqs = (await session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == lockdown_notify_group.chat_id,
            ).order_by(JoinRequest.id.asc())
        )).scalars().all()
        assert len(reqs) == 2
        assert reqs[0].decision == DecisionEnum.rejected
        assert reqs[1].decision == DecisionEnum.manual_review


@pytest.mark.asyncio
async def test_manual_review_resolved_via_callback_allows_new_request(
    test_session_factory, lockdown_notify_group, mock_bot, setup_bot_engines
):
    user = make_tg_user(user_id=20008, first_name="FlowUser")
    req1 = make_join_request(user=user)

    # 1. First join request enters manual review
    await handle_join_request(req1, mock_bot, group_db=lockdown_notify_group)

    async with test_session_factory() as session:
        res = await session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == lockdown_notify_group.chat_id,
            )
        )
        initial_join_req = res.scalar_one()
        assert initial_join_req.decision == DecisionEnum.manual_review
        join_req_id = initial_join_req.id

    # 2. Admin reviews and approves the request via callback
    admin = make_tg_user(user_id=lockdown_notify_group.owner_id, username="owner")
    cb = make_callback_query(f"rev:approve:{user.id}:{join_req_id}:{lockdown_notify_group.id}", user=admin)
    await cb_manual_review(cb, mock_bot)

    async with test_session_factory() as session:
        updated_req = await session.get(JoinRequest, join_req_id)
        assert updated_req.decision == DecisionEnum.approved
        assert updated_req.resolved_at is not None

    # 3. User leaves and later sends a second join request
    req2 = make_join_request(user=user)
    await handle_join_request(req2, mock_bot, group_db=lockdown_notify_group)

    # Second request should NOT be blocked by deduplication because the first one is resolved
    async with test_session_factory() as session:
        all_reqs = (await session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.chat_id == lockdown_notify_group.chat_id,
            ).order_by(JoinRequest.id.asc())
        )).scalars().all()
        assert len(all_reqs) == 2
        assert all_reqs[0].decision == DecisionEnum.approved
        assert all_reqs[1].decision == DecisionEnum.manual_review


@pytest.mark.asyncio
async def test_group_db_none_returns_immediately(mock_bot):
    req = make_join_request()
    await handle_join_request(req, mock_bot, group_db=None)
    mock_bot.send_message.assert_not_called()
    mock_bot.approve_chat_join_request.assert_not_called()
