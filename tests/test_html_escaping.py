"""Regression tests for the Telegram HTML-parse-crash bug class.

Background: DefaultBotProperties(parse_mode=ParseMode.HTML) means every
message.reply(...)/send_message(...) that doesn't pass its own parse_mode is
parsed as HTML. Any interpolated value that isn't ours - a group title, a
user's name, admin-typed text, a raw Telegram error string - can carry a
stray '<' and make Telegram reject the whole message with "can't parse
entities", silently killing the update. This file proves the paths touched
by the fix now escape those values instead of sending them raw.

The two canonical poison inputs used throughout: a group titled
"Community <3 Animals" and a user whose first name is "<b>bold</b>".
"""
import pytest
from unittest.mock import AsyncMock

from aiogram.types import Message

import bot.handlers.lists_menu
from bot.handlers.settings_menu import cmd_threshold
from bot.handlers.superadmin import cmd_search_user, cmd_broadcast
from bot.handlers.lists_menu import fsm_bl_enter_score
from bot.handlers.join_requests import handle_join_request
from database.models import Group, GroupConfig, DecisionEnum
from tests.conftest import make_join_request, make_tg_user, make_tg_chat
from config import settings

MALICIOUS_TITLE = "Community <3 Animals"
MALICIOUS_NAME = "<b>bold</b>"


# ─── bot/handlers/join_requests.py: _notify_manual_review ─────────────────────
# This is the manual-review admin DM that interpolates group.title and the
# user's first/last name directly into HTML text (the production bug's
# sibling: "/ban <@yael43>" crashed a reply the same way a poisoned group
# title or name would crash this notification).

@pytest.mark.asyncio
async def test_manual_review_notification_escapes_group_title_and_user_name(
    test_session_factory, db_session, mock_bot, setup_bot_engines,
):
    group = Group(chat_id=-100999, title=MALICIOUS_TITLE, owner_id=555, is_active=True)
    db_session.add(group)
    await db_session.commit()
    await db_session.refresh(group)

    db_session.add(GroupConfig(group_id=group.id, key="security_mode", value="lockdown", value_type="string"))
    db_session.add(GroupConfig(
        group_id=group.id, key="notify_admin_on_manual_review", value="true", value_type="bool",
    ))
    await db_session.commit()

    user = make_tg_user(user_id=20001, first_name=MALICIOUS_NAME, username=None)
    req = make_join_request(user=user, chat=make_tg_chat(chat_id=group.chat_id))

    await handle_join_request(req, mock_bot, group_db=group)

    admin_calls = [
        call for call in mock_bot.send_message.call_args_list
        if call.args and call.args[0] == group.owner_id
    ]
    assert len(admin_calls) == 1
    sent_text = admin_calls[0].args[1]

    assert MALICIOUS_TITLE not in sent_text
    assert "Community &lt;3 Animals" in sent_text
    assert "<b>bold</b>" not in sent_text
    assert "&lt;b&gt;bold&lt;/b&gt;" in sent_text


# ─── tracking/channel.py: send_decision ────────────────────────────────────────
# The tracking-channel message interpolates group_title, user_name, username
# and a free-text reason (which can be a raw Telegram error string) straight
# into HTML text.

@pytest.mark.asyncio
async def test_send_decision_escapes_group_title_name_and_reason(mock_bot, monkeypatch):
    from tracking.channel import send_decision

    monkeypatch.setattr(settings, "tracking_channel_id", -100555)

    # banned + reason takes the branch that omits username and shows `reason`
    # (a value that can be a raw Telegram error string) instead.
    await send_decision(
        mock_bot, DecisionEnum.banned, 999, MALICIOUS_NAME, None,
        -150.0, [], MALICIOUS_TITLE, None, reason="<script>bad</script>",
    )

    mock_bot.send_message.assert_called_once()
    sent_text = mock_bot.send_message.call_args.args[1]

    assert MALICIOUS_TITLE not in sent_text
    assert "Community &lt;3 Animals" in sent_text
    assert "<b>bold</b>" not in sent_text
    assert "&lt;b&gt;bold&lt;/b&gt;" in sent_text
    assert "<script>" not in sent_text
    assert "&lt;script&gt;bad&lt;/script&gt;" in sent_text


@pytest.mark.asyncio
async def test_send_decision_escapes_username_in_non_reason_branch(mock_bot, monkeypatch):
    from tracking.channel import send_decision

    monkeypatch.setattr(settings, "tracking_channel_id", -100555)

    # manual_review (no reason) takes the branch that shows the username.
    await send_decision(
        mock_bot, DecisionEnum.manual_review, 999, MALICIOUS_NAME, "ok<3user",
        10.0, [], MALICIOUS_TITLE, None,
    )

    mock_bot.send_message.assert_called_once()
    sent_text = mock_bot.send_message.call_args.args[1]

    assert "ok<3user" not in sent_text
    assert "ok&lt;3user" in sent_text


# ─── recovery/sweep.py: _send_owner_summary ────────────────────────────────────
# The post-outage owner DM interpolates group_title the same way.

@pytest.mark.asyncio
async def test_recovery_owner_summary_escapes_group_title(mock_bot):
    from datetime import datetime, timezone
    from recovery.sweep import _send_owner_summary

    summary = {"recovered": 1, "extended": 0, "skipped": 0}
    await _send_owner_summary(
        mock_bot, MALICIOUS_TITLE, -100777, 555,
        datetime.now(timezone.utc), 120, summary,
    )

    mock_bot.send_message.assert_called_once()
    sent_text = mock_bot.send_message.call_args.args[1]

    assert MALICIOUS_TITLE not in sent_text
    assert "Community &lt;3 Animals" in sent_text


# ─── Hardcoded usage strings with literal angle brackets ──────────────────────
# "/threshold <key> <value>", "/search_user <telegram_id>" and
# "/broadcast <message>" are sent via message.reply()/edit_text() under the
# bot's default HTML parse mode. Unescaped, the literal "<" in these
# constants alone is enough to crash the reply to a bare "/threshold" etc.

@pytest.mark.asyncio
async def test_threshold_usage_message_escapes_literal_angle_brackets():
    message = AsyncMock(spec=Message)
    message.text = "/threshold"
    message.reply = AsyncMock()

    await cmd_threshold(message)

    sent_text = message.reply.call_args.args[0]
    assert "<מפתח>" not in sent_text
    assert "&lt;מפתח&gt;" in sent_text


@pytest.mark.asyncio
async def test_search_user_usage_message_escapes_literal_angle_brackets(monkeypatch):
    admin = make_tg_user(user_id=424242, username="admin")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    message = AsyncMock(spec=Message)
    message.text = "/search_user"
    message.from_user = admin
    message.reply = AsyncMock()

    await cmd_search_user(message)

    sent_text = message.reply.call_args.args[0]
    assert "<telegram_id>" not in sent_text
    assert "&lt;telegram_id&gt;" in sent_text


@pytest.mark.asyncio
async def test_broadcast_usage_message_escapes_literal_angle_brackets(monkeypatch):
    admin = make_tg_user(user_id=424242, username="admin")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    message = AsyncMock(spec=Message)
    message.text = "/broadcast"
    message.from_user = admin
    message.reply = AsyncMock()

    await cmd_broadcast(message, AsyncMock())

    sent_text = message.reply.call_args.args[0]
    assert "<הודעה>" not in sent_text
    assert "&lt;הודעה&gt;" in sent_text


# ─── bot/handlers/superadmin.py: cmd_broadcast ─────────────────────────────────
# The broadcast text itself is admin-typed free text, forwarded verbatim to
# every group owner's private chat under HTML parse mode.

@pytest.mark.asyncio
async def test_broadcast_message_escapes_admin_typed_text(
    test_session_factory, db_session, mock_bot, monkeypatch,
):
    admin = make_tg_user(user_id=424242, username="admin")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    group = Group(chat_id=-100321, title="G", owner_id=777, is_active=True)
    db_session.add(group)
    await db_session.commit()

    message = AsyncMock(spec=Message)
    message.text = "/broadcast <b>free</b> stuff <script>bad</script>"
    message.from_user = admin
    message.reply = AsyncMock()

    await cmd_broadcast(message, mock_bot)

    owner_calls = [
        call for call in mock_bot.send_message.call_args_list
        if call.args and call.args[0] == group.owner_id
    ]
    assert len(owner_calls) == 1
    sent_text = owner_calls[0].args[1]
    assert "<script>" not in sent_text
    assert "&lt;script&gt;bad&lt;/script&gt;" in sent_text


# ─── bot/handlers/lists_menu.py: fsm_bl_enter_score ────────────────────────────
# The blacklist-add confirmation echoes the admin-typed keyword back into an
# HTML message via message.answer() (no explicit parse_mode, so it still
# uses the bot's default HTML mode).

@pytest.mark.asyncio
async def test_blacklist_add_confirmation_escapes_keyword(
    test_session_factory, db_session, sample_group, monkeypatch,
):
    monkeypatch.setattr(bot.handlers.lists_menu, "AsyncSessionLocal", test_session_factory)

    state = AsyncMock()
    state.get_data = AsyncMock(return_value={"group_id": sample_group.id, "keyword": "<img src=x>"})
    state.clear = AsyncMock()

    message = AsyncMock(spec=Message)
    message.text = ""
    message.from_user = make_tg_user(user_id=1, username="admin")
    message.answer = AsyncMock()

    await fsm_bl_enter_score(message, state)

    sent_text = message.answer.call_args.args[0]
    assert "<img src=x>" not in sent_text
    assert "&lt;img src=x&gt;" in sent_text
