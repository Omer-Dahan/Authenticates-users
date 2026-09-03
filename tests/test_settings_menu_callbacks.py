import pytest
from sqlalchemy import select
from database.models import GroupConfig, Group
from bot.handlers.settings_menu import cb_notify_menu, cb_set_notify
from tests.conftest import make_callback_query, make_tg_user


@pytest.mark.asyncio
async def test_cb_notify_menu_default_disabled(test_session_factory, sample_group):
    cb = make_callback_query(f"sm:notify:{sample_group.id}")

    await cb_notify_menu(cb)

    cb.answer.assert_awaited_once()
    cb.message.edit_text.assert_awaited_once()
    args, kwargs = cb.message.edit_text.call_args
    text = args[0]
    assert "🔕 כבוי" in text
    reply_markup = kwargs.get("reply_markup")
    assert reply_markup is not None
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    assert any(b.callback_data == f"sm:set_notify:{sample_group.id}:on" for b in buttons)


@pytest.mark.asyncio
async def test_cb_notify_menu_when_already_enabled(test_session_factory, sample_group, db_session):
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="notify_admin_on_manual_review",
        value="true",
        value_type="bool",
    ))
    await db_session.commit()

    cb = make_callback_query(f"sm:notify:{sample_group.id}")
    await cb_notify_menu(cb)

    cb.answer.assert_awaited_once()
    args, kwargs = cb.message.edit_text.call_args
    text = args[0]
    assert "🔔 פעיל" in text
    reply_markup = kwargs.get("reply_markup")
    buttons = [btn for row in reply_markup.inline_keyboard for btn in row]
    assert any(b.callback_data == f"sm:set_notify:{sample_group.id}:off" for b in buttons)


@pytest.mark.asyncio
@pytest.mark.parametrize("truthy_val", ["1", "yes", "YES", "True"])
async def test_cb_notify_menu_truthy_variants(test_session_factory, sample_group, db_session, truthy_val):
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="notify_admin_on_manual_review",
        value=truthy_val,
        value_type="bool",
    ))
    await db_session.commit()

    cb = make_callback_query(f"sm:notify:{sample_group.id}")
    await cb_notify_menu(cb)

    args, _ = cb.message.edit_text.call_args
    assert "🔔 פעיל" in args[0]


@pytest.mark.asyncio
async def test_cb_notify_menu_with_none_value_does_not_crash(test_session_factory, sample_group, db_session):
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="notify_admin_on_manual_review",
        value=None,
        value_type="bool",
    ))
    await db_session.commit()

    cb = make_callback_query(f"sm:notify:{sample_group.id}")
    # Should safely display disabled instead of raising AttributeError
    await cb_notify_menu(cb)

    cb.answer.assert_awaited_once()
    args, _ = cb.message.edit_text.call_args
    assert "🔕 כבוי" in args[0]


@pytest.mark.asyncio
async def test_cb_set_notify_on(test_session_factory, sample_group):
    admin = make_tg_user(user_id=777, first_name="OwnerAdmin", username="owner_adm")
    cb = make_callback_query(f"sm:set_notify:{sample_group.id}:on", user=admin)

    await cb_set_notify(cb)

    cb.answer.assert_awaited_once_with("התראות סקירה ידנית: 🔔 פעיל", show_alert=True)
    cb.message.edit_text.assert_awaited_once()
    args, kwargs = cb.message.edit_text.call_args
    assert "🔔 <b>התראות סקירה ידנית שונו ל:</b> 🔔 פעיל" in args[0]

    # Verify DB state
    async with test_session_factory() as session:
        res = await session.execute(
            select(GroupConfig).where(
                GroupConfig.group_id == sample_group.id,
                GroupConfig.key == "notify_admin_on_manual_review",
            )
        )
        cfg = res.scalar_one_or_none()
        assert cfg is not None
        assert cfg.value == "true"
        assert cfg.value_type == "bool"

        grp = await session.get(Group, sample_group.id)
        assert grp.settings_last_edited_by_id == 777


@pytest.mark.asyncio
async def test_cb_set_notify_off(test_session_factory, sample_group, db_session):
    # Start enabled
    db_session.add(GroupConfig(
        group_id=sample_group.id,
        key="notify_admin_on_manual_review",
        value="true",
        value_type="bool",
    ))
    await db_session.commit()

    admin = make_tg_user(user_id=888, first_name="SecondAdmin", username="sec_adm")
    cb = make_callback_query(f"sm:set_notify:{sample_group.id}:off", user=admin)

    await cb_set_notify(cb)

    cb.answer.assert_awaited_once_with("התראות סקירה ידנית: 🔕 כבוי", show_alert=True)
    cb.message.edit_text.assert_awaited_once()
    args, _ = cb.message.edit_text.call_args
    assert "🔕 כבוי" in args[0]

    # Verify DB state
    async with test_session_factory() as session:
        res = await session.execute(
            select(GroupConfig).where(
                GroupConfig.group_id == sample_group.id,
                GroupConfig.key == "notify_admin_on_manual_review",
            )
        )
        cfg = res.scalar_one_or_none()
        assert cfg is not None
        assert cfg.value == "false"
        assert cfg.value_type == "bool"

        grp = await session.get(Group, sample_group.id)
        assert grp.settings_last_edited_by_id == 888


@pytest.mark.asyncio
async def test_cb_set_notify_invalid_state(test_session_factory, sample_group, db_session):
    cb = make_callback_query(f"sm:set_notify:{sample_group.id}:toggle")

    await cb_set_notify(cb)

    cb.answer.assert_awaited_once_with("ערך לא חוקי.")
    cb.message.edit_text.assert_not_called()

    # DB should not have been updated
    async with test_session_factory() as session:
        res = await session.execute(
            select(GroupConfig).where(
                GroupConfig.group_id == sample_group.id,
                GroupConfig.key == "notify_admin_on_manual_review",
            )
        )
        assert res.scalar_one_or_none() is None
