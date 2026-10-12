"""Regression tests for purging pending join requests from deleted accounts
(docs/BAN.md, section "Purging pending requests from deleted accounts")."""
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from aiogram.exceptions import TelegramAPIError
from aiogram.types import User, ChatMemberMember, Message

from database.models import DecisionEnum, JoinRequest, ModerationLog, TelegramUser, VerificationSession
from moderation.actions import purge_deleted_requests, PurgeReport
import bot.handlers.ban_commands as ban_commands_module
from bot.handlers.ban_commands import cb_purge_apply, format_purge_report
from tests.conftest import make_tg_user, make_callback_query, drain_purge_tasks


def _deleted_member() -> ChatMemberMember:
    user = User(id=0, is_bot=False, first_name="", username=None, last_name=None)
    return ChatMemberMember(status="member", user=user)


async def _make_pending_request(db_session, group, user_id, name="Michael Thompson"):
    db_session.add(TelegramUser(telegram_id=user_id, first_name=name))
    jr = JoinRequest(
        group_id=group.id, user_id=user_id, chat_id=group.chat_id,
        decision=DecisionEnum.pending, score=10.0, matched_rules=[],
    )
    db_session.add(jr)
    session = VerificationSession(user_id=user_id, chat_id=group.chat_id, completed_at=None)
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(jr)
    await db_session.refresh(session)
    return jr, session


# ─── 1. A confirmed deleted account is declined and the row closed ───────────

@pytest.mark.asyncio
async def test_purge_declines_and_closes_confirmed_deleted_account(
    test_session_factory, sample_group, db_session, mock_bot
):
    user_id = 8503461118
    jr, session = await _make_pending_request(db_session, sample_group, user_id)

    mock_bot.get_chat_member.return_value = _deleted_member()

    report = await purge_deleted_requests(
        mock_bot, dry_run=False, actor_id=999, actor_name="admin", db=db_session,
    )

    assert (sample_group.title, user_id, "Michael Thompson") in report.deleted_found
    assert (sample_group.title, user_id) in report.removed
    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, user_id)

    async with test_session_factory() as check_db:
        refreshed_jr = await check_db.get(JoinRequest, jr.id)
        assert refreshed_jr.decision == DecisionEnum.rejected
        assert refreshed_jr.resolved_at is not None
        assert refreshed_jr.notes == "account deleted (empty profile)"

        refreshed_session = await check_db.get(VerificationSession, session.id)
        assert refreshed_session.completed_at is not None
        assert refreshed_session.passed is False

        logs = (await check_db.execute(
            select(ModerationLog).where(ModerationLog.user_id == user_id)
        )).scalars().all()
        assert len(logs) == 1
        assert logs[0].details.get("purge_deleted") is True


# ─── 2. "member not found" is routine - leaves the row untouched, inconclusive

@pytest.mark.asyncio
async def test_purge_member_not_found_is_inconclusive_and_untouched(
    test_session_factory, sample_group, db_session, mock_bot
):
    user_id = 8503461119
    jr, _session = await _make_pending_request(db_session, sample_group, user_id)

    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Bad Request: member not found"
    )
    mock_bot.get_chat.side_effect = TelegramAPIError(
        method="getChat", message="Bad Request: chat not found"
    )

    report = await purge_deleted_requests(
        mock_bot, dry_run=False, actor_id=999, actor_name="admin", db=db_session,
    )

    assert report.inconclusive == 1
    assert report.deleted_found == []
    assert report.access_errors == []
    mock_bot.decline_chat_join_request.assert_not_called()

    async with test_session_factory() as check_db:
        refreshed_jr = await check_db.get(JoinRequest, jr.id)
        assert refreshed_jr.decision == DecisionEnum.pending
        assert refreshed_jr.resolved_at is None


# ─── 3. "not enough rights" is an access problem - row stays open, access_errors

@pytest.mark.asyncio
async def test_purge_not_enough_rights_leaves_row_open_and_reports_access_error(
    test_session_factory, sample_group, db_session, mock_bot
):
    user_id = 8503461120
    jr, _session = await _make_pending_request(db_session, sample_group, user_id)

    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Bad Request: not enough rights"
    )

    report = await purge_deleted_requests(
        mock_bot, dry_run=False, actor_id=999, actor_name="admin", db=db_session,
    )

    assert report.inconclusive == 1
    assert len(report.access_errors) == 1
    assert report.access_errors[0][0] == sample_group.title
    mock_bot.get_chat.assert_not_called()
    mock_bot.decline_chat_join_request.assert_not_called()

    async with test_session_factory() as check_db:
        refreshed_jr = await check_db.get(JoinRequest, jr.id)
        assert refreshed_jr.decision == DecisionEnum.pending
        assert refreshed_jr.resolved_at is None


# ─── 4. dry_run=True performs zero mutating Telegram calls ───────────────────

@pytest.mark.asyncio
async def test_purge_dry_run_makes_zero_mutating_calls(
    test_session_factory, sample_group, db_session, mock_bot
):
    user_id = 8503461121
    jr, _session = await _make_pending_request(db_session, sample_group, user_id)

    mock_bot.get_chat_member.return_value = _deleted_member()

    report = await purge_deleted_requests(
        mock_bot, dry_run=True, actor_id=999, actor_name="admin", db=db_session,
    )

    assert (sample_group.title, user_id, "Michael Thompson") in report.deleted_found
    mock_bot.decline_chat_join_request.assert_not_called()

    async with test_session_factory() as check_db:
        refreshed_jr = await check_db.get(JoinRequest, jr.id)
        assert refreshed_jr.decision == DecisionEnum.pending
        assert refreshed_jr.resolved_at is None


# ─── 5. The confirm button does nothing without the second press, and refuses
#        a non-super-admin ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_command_first_press_is_dry_run_only(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    admin = make_tg_user(user_id=987654321, first_name="Admin", username="adminuser")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    user_id = 8503461122
    await _make_pending_request(db_session, sample_group, user_id)
    mock_bot.get_chat_member.return_value = _deleted_member()

    message = AsyncMock(spec=Message)
    message.text = "/purge_deleted"
    message.from_user = admin
    message.reply = AsyncMock()

    await ban_commands_module.cmd_purge_deleted(message, mock_bot)

    mock_bot.decline_chat_join_request.assert_not_called()
    message.reply.assert_called_once()
    _args, kwargs = message.reply.call_args
    assert kwargs.get("reply_markup") is not None

    async with test_session_factory() as check_db:
        ban_rows = (await check_db.execute(
            select(JoinRequest).where(JoinRequest.user_id == user_id)
        )).scalars().all()
        assert ban_rows[0].decision == DecisionEnum.pending


@pytest.mark.asyncio
async def test_purge_command_global_dry_run_emits_scoped_apply_all_button(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    """The command's global path (no chat_id argument) was untested: this
    pins its button down to the scoped `purge:apply:all` token, not the old
    unscoped `purge:apply`."""
    from config import settings
    admin = make_tg_user(user_id=987654321, first_name="Admin", username="adminuser")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    user_id = 8503461126
    await _make_pending_request(db_session, sample_group, user_id)
    mock_bot.get_chat_member.return_value = _deleted_member()

    message = AsyncMock(spec=Message)
    message.text = "/purge_deleted"
    message.from_user = admin
    message.reply = AsyncMock()

    await ban_commands_module.cmd_purge_deleted(message, mock_bot)

    _args, kwargs = message.reply.call_args
    markup = kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert callback_datas == ["purge:apply:all"]
    assert ban_commands_module._PENDING_PURGES[admin.id]["group_id"] is None


@pytest.mark.asyncio
async def test_purge_apply_callback_refuses_non_super_admin(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)  # distinct from the presser

    user_id = 8503461123
    await _make_pending_request(db_session, sample_group, user_id)
    mock_bot.get_chat_member.return_value = _deleted_member()

    attacker = make_tg_user(user_id=99999, first_name="Random", username="randomguy")
    callback = make_callback_query("purge:apply:0:1", user=attacker)

    await cb_purge_apply(callback, mock_bot)

    callback.answer.assert_called_once()
    _args, kwargs = callback.answer.call_args
    answer_text = callback.answer.call_args.args[0] if callback.answer.call_args.args else kwargs.get("text", "")
    assert "הרשא" in answer_text
    mock_bot.decline_chat_join_request.assert_not_called()
    mock_bot.get_chat_member.assert_not_called()
    callback.message.edit_text.assert_not_called()

    async with test_session_factory() as check_db:
        refreshed = (await check_db.execute(
            select(JoinRequest).where(JoinRequest.user_id == user_id)
        )).scalars().first()
        assert refreshed.decision == DecisionEnum.pending


# ─── 6. Pressing confirm twice runs the real purge exactly once; the second
#        press is refused because the dry-run record was already consumed ────

@pytest.mark.asyncio
async def test_purge_apply_second_press_is_refused(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    admin = make_tg_user(user_id=987654321, first_name="Admin", username="adminuser")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    user_id = 8503461124
    await _make_pending_request(db_session, sample_group, user_id)
    mock_bot.get_chat_member.return_value = _deleted_member()

    message = AsyncMock(spec=Message)
    message.text = "/purge_deleted"
    message.from_user = admin
    message.reply = AsyncMock()

    await ban_commands_module.cmd_purge_deleted(message, mock_bot)

    callback = make_callback_query("purge:apply:all", user=admin)
    await cb_purge_apply(callback, mock_bot)
    await drain_purge_tasks()

    assert mock_bot.decline_chat_join_request.call_count == 1

    second_callback = make_callback_query("purge:apply:all", user=admin)
    await cb_purge_apply(second_callback, mock_bot)

    assert mock_bot.decline_chat_join_request.call_count == 1
    second_callback.answer.assert_called_once()
    answer_text = (
        second_callback.answer.call_args.args[0]
        if second_callback.answer.call_args.args
        else second_callback.answer.call_args.kwargs.get("text", "")
    )
    assert "purge_deleted" in answer_text
    second_callback.message.edit_text.assert_not_called()


# ─── 7. A confirm with no pending dry-run record refuses and performs no
#        Telegram mutating call ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_apply_without_pending_record_refuses(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    admin = make_tg_user(user_id=987654322, first_name="Admin", username="adminuser")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    user_id = 8503461125
    await _make_pending_request(db_session, sample_group, user_id)
    mock_bot.get_chat_member.return_value = _deleted_member()

    callback = make_callback_query("purge:apply", user=admin)
    await cb_purge_apply(callback, mock_bot)

    callback.answer.assert_called_once()
    answer_text = (
        callback.answer.call_args.args[0]
        if callback.answer.call_args.args
        else callback.answer.call_args.kwargs.get("text", "")
    )
    assert "purge_deleted" in answer_text
    mock_bot.decline_chat_join_request.assert_not_called()
    mock_bot.get_chat_member.assert_not_called()
    callback.message.edit_text.assert_not_called()

    async with test_session_factory() as check_db:
        refreshed = (await check_db.execute(
            select(JoinRequest).where(JoinRequest.user_id == user_id)
        )).scalars().first()
        assert refreshed.decision == DecisionEnum.pending


# ─── 8. format_purge_report shows a per-group breakdown, not just totals ─────
# PurgeReport.per_group already carries per-group counters; the owner could
# not tell which group they belonged to until format_purge_report rendered
# them.

def test_format_purge_report_includes_per_group_breakdown():
    report = PurgeReport(
        checked=5,
        deleted_found=[("Group A", 1, "Name One"), ("Group B", 2, None)],
        removed=[("Group A", 1)],
        alive=1,
        inconclusive=1,
        per_group={
            "Group A": {
                "checked": 3, "deleted_found": 1, "removed": 1,
                "alive": 1, "inconclusive": 0, "access_error": None,
            },
            "Group B": {
                "checked": 2, "deleted_found": 1, "removed": 0,
                "alive": 0, "inconclusive": 1, "access_error": "Bad Request: not enough rights",
            },
        },
    )

    text = format_purge_report(report, applied=True)

    assert "Group A" in text
    assert "Group B" in text
    assert "נבדקו: 3" in text
    assert "נבדקו: 2" in text
    assert "מחוקים: 1" in text
    assert "שגיאת גישה: Bad Request: not enough rights" in text
