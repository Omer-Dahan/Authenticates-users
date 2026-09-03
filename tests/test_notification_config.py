import pytest
from sqlalchemy import select
from database.models import GroupConfig, JoinRequest, DecisionEnum
from bot.handlers.join_requests import handle_join_request
from tests.conftest import make_join_request, make_tg_user


@pytest.fixture
async def lockdown_group(sample_group, db_session):
    """Configures sample_group with lockdown mode so every join request results in manual_review."""
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="security_mode",
        value="lockdown",
        value_type="string",
    ))
    await db_session.commit()
    return sample_group


@pytest.mark.asyncio
async def test_manual_review_missing_config_does_not_notify_admin(
    test_session_factory, lockdown_group, mock_bot, setup_bot_engines
):
    user = make_tg_user(user_id=10001, first_name="Alice")
    req = make_join_request(user=user)

    await handle_join_request(req, mock_bot, group_db=lockdown_group)

    # Verify request was stored as manual_review
    async with test_session_factory() as session:
        res = await session.execute(
            select(JoinRequest).where(
                JoinRequest.user_id == user.id,
                JoinRequest.group_id == lockdown_group.id,
            )
        )
        join_req = res.scalar_one_or_none()
        assert join_req is not None
        assert join_req.decision == DecisionEnum.manual_review

    # Admin was NOT sent a private review message
    admin_notified = any(
        call.args and call.args[0] == lockdown_group.owner_id
        for call in mock_bot.send_message.call_args_list
    )
    assert not admin_notified


@pytest.mark.asyncio
async def test_manual_review_config_false_does_not_notify_admin(
    test_session_factory, lockdown_group, mock_bot, setup_bot_engines, db_session
):
    db_session.add(GroupConfig(
        group_id=lockdown_group.id,
        key="notify_admin_on_manual_review",
        value="false",
        value_type="bool",
    ))
    await db_session.commit()

    user = make_tg_user(user_id=10002, first_name="Bob")
    req = make_join_request(user=user)

    await handle_join_request(req, mock_bot, group_db=lockdown_group)

    # Admin was NOT notified
    admin_notified = any(
        call.args and call.args[0] == lockdown_group.owner_id
        for call in mock_bot.send_message.call_args_list
    )
    assert not admin_notified


@pytest.mark.asyncio
async def test_manual_review_config_true_notifies_admin(
    test_session_factory, lockdown_group, mock_bot, setup_bot_engines, db_session
):
    db_session.add(GroupConfig(
        group_id=lockdown_group.id,
        key="notify_admin_on_manual_review",
        value="true",
        value_type="bool",
    ))
    await db_session.commit()

    user = make_tg_user(user_id=10003, first_name="Charlie", username="charlie_tg")
    req = make_join_request(user=user)

    await handle_join_request(req, mock_bot, group_db=lockdown_group)

    # Admin WAS notified
    admin_calls = [
        call for call in mock_bot.send_message.call_args_list
        if call.args and call.args[0] == lockdown_group.owner_id
    ]
    assert len(admin_calls) == 1

    call = admin_calls[0]
    sent_text = call.args[1]
    assert "⚠️ <b>סקירה ידנית נדרשת</b>" in sent_text
    assert "Charlie" in sent_text
    assert "@charlie_tg" in sent_text
    assert "ID: <code>10003</code>" in sent_text

    # Verify review keyboard markup is attached
    reply_markup = call.kwargs.get("reply_markup")
    assert reply_markup is not None
    buttons = [b for row in reply_markup.inline_keyboard for b in row]
    assert any("אשר" in b.text for b in buttons)
    assert any("דחה" in b.text for b in buttons)
    assert any("חסום" in b.text for b in buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize("truthy_val", ["1", "yes", "YES"])
async def test_manual_review_truthy_variants_notify_admin(
    test_session_factory, lockdown_group, mock_bot, setup_bot_engines, db_session, truthy_val
):
    db_session.add(GroupConfig(
        group_id=lockdown_group.id,
        key="notify_admin_on_manual_review",
        value=truthy_val,
        value_type="bool",
    ))
    await db_session.commit()

    user = make_tg_user(user_id=10004, first_name="David")
    req = make_join_request(user=user)

    await handle_join_request(req, mock_bot, group_db=lockdown_group)

    admin_calls = [
        call for call in mock_bot.send_message.call_args_list
        if call.args and call.args[0] == lockdown_group.owner_id
    ]
    assert len(admin_calls) == 1
