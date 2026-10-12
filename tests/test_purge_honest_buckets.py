"""Regression tests for the honest bucketing of the deleted-account purge and for
the per-group purge screen in the settings menu (docs/BAN.md, section "Purging
pending requests from deleted accounts")."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message, User, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated

from database.models import DecisionEnum, Group, JoinRequest, TelegramUser, UserBan, VerificationSession
from moderation.actions import purge_deleted_requests, PurgeReport
import bot.handlers.ban_commands as ban_commands_module
import bot.handlers.member_events as member_events
from bot.handlers.ban_commands import cb_purge_apply, cmd_purge_deleted, format_purge_report
from bot.handlers.settings_menu import cb_purge_menu, cb_purge_apply_group, cb_manual_review
from bot.keyboards.settings_kb import main_settings_kb
from tests.conftest import make_tg_user, make_tg_chat, make_callback_query, drain_purge_tasks


@pytest.fixture(autouse=True)
def _clear_pending_purges():
    ban_commands_module._PENDING_PURGES.clear()
    yield
    ban_commands_module._PENDING_PURGES.clear()


def _deleted_member() -> ChatMemberMember:
    user = User(id=0, is_bot=False, first_name="", username=None, last_name=None)
    return ChatMemberMember(status="member", user=user)


def _left_named(user_id: int) -> ChatMemberLeft:
    return ChatMemberLeft(status="left", user=User(id=user_id, is_bot=False, first_name="Dana"))


def _joined_member(user_id: int) -> ChatMemberMember:
    return ChatMemberMember(status="member", user=User(id=user_id, is_bot=False, first_name="Dana"))


async def _open_row(db_session, group, user_id, decision=DecisionEnum.pending):
    db_session.add(TelegramUser(telegram_id=user_id, first_name="Dana"))
    jr = JoinRequest(
        group_id=group.id, user_id=user_id, chat_id=group.chat_id,
        decision=decision, score=10.0, matched_rules=[],
    )
    db_session.add(jr)
    await db_session.commit()
    await db_session.refresh(jr)
    return jr


def _missing_request_error() -> TelegramAPIError:
    return TelegramAPIError(method="declineChatJoinRequest", message="Bad Request: HIDE_REQUESTER_MISSING")


# ─── Bucketing ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_already_member_is_reported_and_closed_only_on_apply_without_any_decline(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _open_row(db_session, sample_group, 7001)
    mock_bot.get_chat_member.return_value = _joined_member(7001)

    dry = await purge_deleted_requests(mock_bot, dry_run=True, db=db_session)
    assert dry.already_member == [(sample_group.title, 7001)]
    assert dry.alive == 0
    async with test_session_factory() as check_db:
        assert (await check_db.get(JoinRequest, jr.id)).resolved_at is None

    applied = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)
    assert applied.already_member == [(sample_group.title, 7001)]
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.approved
        assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_deleted_account_whose_request_telegram_no_longer_has_is_counted_apart(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _open_row(db_session, sample_group, 7002)
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = _missing_request_error()

    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert report.removed == []
    assert report.request_gone == [(sample_group.title, 7002)]
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_deleted_account_whose_decline_is_refused_is_cleared_via_ban_fallback(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Reproduces the production failure: Telegram refuses declineChatJoinRequest
    with "Forbidden: user is deactivated" even though the request is still
    sitting in the group's pending list. The sweep must escalate to
    banChatMember and, if that succeeds, actually clear the request from
    Telegram instead of quietly leaving it there."""
    jr = await _open_row(db_session, sample_group, 7003)
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )

    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert report.removed == []
    assert report.request_gone == []
    assert report.decline_failed == []
    assert report.removed_via_ban == [(sample_group.title, 7003)]
    mock_bot.ban_chat_member.assert_called_once_with(sample_group.chat_id, 7003)
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.cleared_via_ban
        assert row.resolved_at is not None

        # The ban must be recorded the same way every other ban access path
        # records one, so /unban can find it and a future admin can see what
        # happened.
        ban_row = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == 7003, UserBan.lifted_at.is_(None))
        )).scalars().first()
        assert ban_row is not None
        assert ban_row.scope == "local"
        assert ban_row.group_id == sample_group.id


@pytest.mark.asyncio
async def test_deleted_account_whose_decline_is_refused_for_a_non_deactivation_reason_never_bans(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Finding D: only an explicit "user is deactivated" refusal justifies
    the ban fallback. A rights error under a different string must leave the
    row open and reported as a failure, never trigger a ban."""
    jr = await _open_row(db_session, sample_group, 7007)
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: CHAT_ADMIN_REQUIRED"
    )

    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert report.removed_via_ban == []
    assert len(report.decline_failed) == 1
    mock_bot.ban_chat_member.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_unresolvable_account_is_its_own_state_and_stays_open(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Finding B: PARTICIPANT_ID_INVALID/USER_ID_INVALID/"member not found"
    on both getChatMember and the getChat fallback means Telegram has no
    record of this user in this chat at all - not an access problem, not
    proof of deletion, and not a generic probe error either."""
    jr = await _open_row(db_session, sample_group, 7008)
    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Bad Request: PARTICIPANT_ID_INVALID"
    )
    mock_bot.get_chat.side_effect = TelegramAPIError(
        method="getChat", message="Bad Request: chat not found"
    )

    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert report.unresolvable == [(sample_group.title, 7008)]
    assert report.inconclusive == 0
    assert report.alive == 0
    mock_bot.decline_chat_join_request.assert_not_called()
    mock_bot.ban_chat_member.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_deleted_account_stuck_when_both_decline_and_ban_fail_keeps_row_open(
    test_session_factory, sample_group, db_session, mock_bot
):
    """When even the ban fallback is refused, the bot genuinely cannot clear
    the request - the row must stay open, not get closed as if it had
    succeeded."""
    jr = await _open_row(db_session, sample_group, 7005)
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )
    mock_bot.ban_chat_member.side_effect = TelegramAPIError(
        method="banChatMember", message="Forbidden: user is deactivated"
    )

    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert report.removed == []
    assert report.request_gone == []
    assert report.removed_via_ban == []
    assert len(report.decline_failed) == 1
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_live_applicant_is_never_declined_or_closed(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _open_row(db_session, sample_group, 7004)
    mock_bot.get_chat_member.return_value = _left_named(7004)

    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert report.alive == 1
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        assert (await check_db.get(JoinRequest, jr.id)).resolved_at is None


@pytest.mark.asyncio
async def test_per_group_keeps_groups_with_the_same_title_apart(
    sample_group, db_session, mock_bot
):
    twin = Group(chat_id=-1009990001112, title=sample_group.title, owner_id=1, is_active=True)
    db_session.add(twin)
    await db_session.commit()
    await db_session.refresh(twin)
    await _open_row(db_session, sample_group, 7005)
    db_session.add(JoinRequest(
        group_id=twin.id, user_id=7006, chat_id=twin.chat_id,
        decision=DecisionEnum.pending, score=0.0, matched_rules=[],
    ))
    await db_session.commit()
    mock_bot.get_chat_member.return_value = _left_named(7005)

    report = await purge_deleted_requests(mock_bot, dry_run=True, db=db_session)

    assert len(report.per_group) == 2
    assert all(stats["checked"] == 1 for stats in report.per_group.values())


def test_report_wording_separates_bot_rows_from_what_telegram_knows():
    report = PurgeReport(
        checked=449,
        deleted_found=[("G", 1, None), ("G", 2, None)],
        removed=[("G", 1)],
        request_gone=[("G", 2)],
        already_member=[("G", 3)],
        alive=27,
        inconclusive=408,
    )
    text = format_purge_report(report, applied=True)

    assert "בקשות פתוחות במסד הנתונים של הבוט: 449" in text
    assert "טלגרם עדיין הכיר את הבקשה, ונדחתה: 1" in text
    assert "טלגרם כבר לא הכיר את הבקשה, נסגרה אצל הבוט: 1" in text
    assert "כבר חברים בקבוצה, הבקשה נסגרה אצל הבוט: 1" in text
    assert "לא ידוע אם הבקשה קיימת: 408" in text
    assert "נבדקו: 449 בקשות ממתינות" not in text
    assert "המספר שמופיע באפליקציה הוא המספר האמיתי" in text
    assert "\u2014" not in text


def test_report_totals_equal_the_sum_of_their_parts():
    """Finding 5: every displayed total must equal the sum of the lines
    shown under it, both at the top level and in the per-group breakdown -
    including rows whose close lost a race, which must get their own line
    instead of silently inflating deleted_found."""
    stats = {
        "title": "G", "checked": 5, "deleted_found": 4, "removed": 1,
        "request_gone": 1, "removed_via_ban": 1, "decline_failed": 0,
        "race_lost_deleted": 1, "race_lost_already_member": 0, "already_member": 0,
        "unresolvable": 0, "alive": 0, "inconclusive": 1, "access_error": None,
    }
    report = PurgeReport(
        checked=5,
        deleted_found=[("G", 1, None), ("G", 2, None), ("G", 3, None), ("G", 4, None)],
        removed=[("G", 1)],
        request_gone=[("G", 2)],
        removed_via_ban=[("G", 3)],
        race_lost_deleted=[("G", 4)],
        inconclusive=1,
        per_group={1: stats},
    )
    text = format_purge_report(report, applied=True)

    assert len(report.deleted_found) == (
        len(report.removed) + len(report.request_gone) + len(report.removed_via_ban)
        + len(report.decline_failed) + len(report.race_lost_deleted)
    )
    assert "הוכרע במקביל: 1" in text
    assert f"דחייה נכשלה: {stats['decline_failed']}" in text


def test_report_race_lost_already_member_never_inflates_deleted_found():
    """Finding (purge race_lost mixing two row types): a race-lost row can
    come from the already_member path (never counted in deleted_found) or
    from the deleted path (counted in deleted_found). Mixing the two into one
    bucket broke the invariant that deleted_found equals the sum of its own
    breakdown. Trigger from the review: checked=1, deleted_found=0,
    one already_member row that lost its race - that single row must never
    be attributed to the deleted_found breakdown, at either level."""
    stats = {
        "title": "G", "checked": 1, "deleted_found": 0, "removed": 0,
        "request_gone": 0, "removed_via_ban": 0, "decline_failed": 0,
        "race_lost_deleted": 0, "race_lost_already_member": 1, "already_member": 0,
        "unresolvable": 0, "alive": 0, "inconclusive": 0, "access_error": None,
    }
    report = PurgeReport(
        checked=1,
        race_lost_already_member=[("G", 4)],
        per_group={1: stats},
    )
    text = format_purge_report(report, applied=True)

    assert len(report.deleted_found) == (
        len(report.removed) + len(report.request_gone) + len(report.removed_via_ban)
        + len(report.decline_failed) + len(report.race_lost_deleted)
    )
    assert len(report.deleted_found) == 0
    assert len(report.race_lost_already_member) == 1
    assert "בקשה שכבר הייתה חברות הוכרעה על ידי החלטה אחרת בזמן הבדיקה: 1" in text
    assert "בקשת 'כבר חבר' הוכרעה במקביל: 1" in text


def test_report_escapes_the_uid_of_a_nameless_deleted_row():
    report = PurgeReport(checked=1, deleted_found=[("G", 7777, None)])
    text = format_purge_report(report, applied=False)

    assert "  • 7777 (<code>7777</code>) בקבוצה G" in text


def test_report_shows_ban_cleared_and_unresolvable_as_their_own_lines():
    """Finding A: a report whose parts cannot add up to its own total is
    exactly the inconsistency the owner complained about. removed_via_ban
    and unresolvable must each get their own labelled line, not disappear."""
    report = PurgeReport(
        checked=10,
        deleted_found=[("G", 1, None), ("G", 2, None)],
        removed=[],
        request_gone=[],
        removed_via_ban=[("G", 1), ("G", 2)],
        unresolvable=[("G", 3)],
    )
    text = format_purge_report(report, applied=True)

    assert "נוקה באמצעות חסימה: 2" in text
    assert "טלגרם לא מכיר את המשתמש הזה בקבוצה הזו" in text
    assert "רק מי שנכנס לאפליקציה יכול לטפל בה: 1" in text
    assert "—" not in text


# ─── /purge_deleted command ───────────────────────────────────────────────────

def _purge_command(chat_id: int) -> AsyncMock:
    message = AsyncMock(spec=Message)
    message.text = f"/purge_deleted {chat_id}"
    message.from_user = make_tg_user(user_id=1, username="superadmin")
    message.chat = make_tg_chat()
    message.reply = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_purge_command_with_nothing_to_act_on_offers_no_confirm(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    await _open_row(db_session, sample_group, 7030)
    mock_bot.get_chat_member.return_value = _left_named(7030)

    message = _purge_command(sample_group.chat_id)
    await cmd_purge_deleted(message, mock_bot)

    assert message.reply.call_args.kwargs["reply_markup"] is None
    assert "אין מה להסיר." in message.reply.call_args.args[0]
    assert 1 not in ban_commands_module._PENDING_PURGES

    apply = make_callback_query("purge:apply", user=make_tg_user(user_id=1, username="superadmin"))
    await cb_purge_apply(apply, mock_bot)
    mock_bot.decline_chat_join_request.assert_not_called()
    apply.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_purge_command_with_a_deleted_account_offers_confirm_and_removes_nothing(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    jr = await _open_row(db_session, sample_group, 7031)
    mock_bot.get_chat_member.return_value = _deleted_member()

    message = _purge_command(sample_group.chat_id)
    await cmd_purge_deleted(message, mock_bot)

    markup = message.reply.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == [f"purge:apply:{sample_group.id}"]
    assert "בדיקה בלבד, עדיין לא הוסר דבר." in message.reply.call_args.args[0]
    assert ban_commands_module._PENDING_PURGES[1]["group_id"] == sample_group.id
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        assert (await check_db.get(JoinRequest, jr.id)).resolved_at is None


# ─── Per-group settings screen ────────────────────────────────────────────────

def test_main_settings_menu_has_the_purge_row():
    callbacks = [b.callback_data for row in main_settings_kb(42).inline_keyboard for b in row]
    assert "sm:purge:42" in callbacks


def _button_data(callback) -> list:
    markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
    return [b.callback_data for row in markup.inline_keyboard for b in row]


@pytest.mark.asyncio
async def test_settings_purge_first_press_removes_nothing_and_confirm_runs_once(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    owner = make_tg_user(user_id=sample_group.owner_id, username="owner")
    jr = await _open_row(db_session, sample_group, 7010)
    mock_bot.get_chat_member.return_value = _deleted_member()

    first = make_callback_query(f"sm:purge:{sample_group.id}", user=owner)
    await cb_purge_menu(first, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_not_called()
    assert f"sm:purge_apply:{sample_group.id}" in _button_data(first)
    async with test_session_factory() as check_db:
        assert (await check_db.get(JoinRequest, jr.id)).resolved_at is None

    confirm = make_callback_query(f"sm:purge_apply:{sample_group.id}", user=owner)
    await cb_purge_apply_group(confirm, mock_bot)
    await drain_purge_tasks()
    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, 7010)

    again = make_callback_query(f"sm:purge_apply:{sample_group.id}", user=owner)
    await cb_purge_apply_group(again, mock_bot)
    assert mock_bot.decline_chat_join_request.call_count == 1
    again.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_settings_purge_refuses_an_owner_of_another_group(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    await _open_row(db_session, sample_group, 7011)
    mock_bot.get_chat_member.return_value = _deleted_member()
    stranger = make_tg_user(user_id=555000, username="otherowner")

    dry = make_callback_query(f"sm:purge:{sample_group.id}", user=stranger)
    await cb_purge_menu(dry, mock_bot)
    mock_bot.get_chat_member.assert_not_called()
    dry.message.edit_text.assert_not_called()

    apply = make_callback_query(f"sm:purge_apply:{sample_group.id}", user=stranger)
    await cb_purge_apply_group(apply, mock_bot)
    mock_bot.decline_chat_join_request.assert_not_called()


@pytest.mark.asyncio
async def test_settings_purge_confirm_for_a_group_never_shown_is_refused(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    owner = make_tg_user(user_id=sample_group.owner_id, username="owner")
    other = Group(chat_id=-1009990002223, title="Other", owner_id=sample_group.owner_id, is_active=True)
    db_session.add(other)
    await db_session.commit()
    await db_session.refresh(other)
    await _open_row(db_session, sample_group, 7012)
    db_session.add(JoinRequest(
        group_id=other.id, user_id=7013, chat_id=other.chat_id,
        decision=DecisionEnum.pending, score=0.0, matched_rules=[],
    ))
    await db_session.commit()
    mock_bot.get_chat_member.return_value = _deleted_member()

    # No dry run at all: the confirm is refused.
    cold = make_callback_query(f"sm:purge_apply:{sample_group.id}", user=owner)
    await cb_purge_apply_group(cold, mock_bot)
    mock_bot.decline_chat_join_request.assert_not_called()

    # Dry run on one group, crafted confirm for the other: refused.
    await cb_purge_menu(make_callback_query(f"sm:purge:{sample_group.id}", user=owner), mock_bot)
    await drain_purge_tasks()
    crafted = make_callback_query(f"sm:purge_apply:{other.id}", user=owner)
    await cb_purge_apply_group(crafted, mock_bot)
    mock_bot.decline_chat_join_request.assert_not_called()
    crafted.message.edit_text.assert_not_called()


# ─── Rows the bot can reconcile ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_join_outside_an_outage_closes_the_open_row(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _open_row(db_session, sample_group, 7020, decision=DecisionEnum.manual_review)
    db_session.add(VerificationSession(user_id=7020, chat_id=sample_group.chat_id, completed_at=None))
    await db_session.commit()
    user = User(id=7020, is_bot=False, first_name="Dana")
    update = ChatMemberUpdated(
        chat=make_tg_chat(chat_id=sample_group.chat_id),
        from_user=user,
        date=datetime.now(timezone.utc),
        old_chat_member=ChatMemberLeft(status="left", user=user),
        new_chat_member=ChatMemberMember(status="member", user=user),
    )

    await member_events.handle_chat_member_update(update, mock_bot)

    mock_bot.send_message.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.approved
        assert row.resolved_at is not None
        sessions = (await check_db.execute(
            select(VerificationSession).where(VerificationSession.user_id == 7020)
        )).scalars().all()
        assert all(s.completed_at is not None for s in sessions)


@pytest.mark.asyncio
async def test_manual_review_on_a_request_already_gone_closes_the_row(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _open_row(db_session, sample_group, 7021, decision=DecisionEnum.manual_review)
    mock_bot.decline_chat_join_request.side_effect = _missing_request_error()
    owner = make_tg_user(user_id=sample_group.owner_id, username="owner")

    cb = make_callback_query(f"rev:reject:7021:{jr.id}:{sample_group.id}", user=owner)
    await cb_manual_review(cb, mock_bot)

    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is not None
        assert row.decision == DecisionEnum.rejected
