import pytest
from bot.keyboards.settings_kb import main_settings_kb, notify_settings_kb


def test_main_settings_kb_has_notify_button():
    group_id = 123
    kb = main_settings_kb(group_id)
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    
    notify_btns = [b for b in buttons if b.callback_data == f"sm:notify:{group_id}"]
    assert len(notify_btns) == 1
    assert "התראות סקירה ידנית" in notify_btns[0].text
    assert "🔔" in notify_btns[0].text


def test_notify_settings_kb_when_enabled():
    group_id = 456
    kb = notify_settings_kb(group_id, enabled=True)
    buttons = [btn for row in kb.inline_keyboard for btn in row]

    toggle_btn = next((b for b in buttons if b.callback_data == f"sm:set_notify:{group_id}:off"), None)
    assert toggle_btn is not None
    assert "כבה התראות" in toggle_btn.text
    assert "🔕" in toggle_btn.text

    # Back and refresh buttons exist
    assert any(b.callback_data == f"sm:main:{group_id}" for b in buttons)
    assert any(b.callback_data == "start:refresh" for b in buttons)


def test_notify_settings_kb_when_disabled():
    group_id = 789
    kb = notify_settings_kb(group_id, enabled=False)
    buttons = [btn for row in kb.inline_keyboard for btn in row]

    toggle_btn = next((b for b in buttons if b.callback_data == f"sm:set_notify:{group_id}:on"), None)
    assert toggle_btn is not None
    assert "הפעל התראות" in toggle_btn.text
    assert "🔔" in toggle_btn.text

    # Back and refresh buttons exist
    assert any(b.callback_data == f"sm:main:{group_id}" for b in buttons)
    assert any(b.callback_data == "start:refresh" for b in buttons)
