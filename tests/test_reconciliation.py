"""Regression tests for the reconciliation sweep that closes join_requests
rows against what Telegram can actually confirm (moderation/actions.py,
run_reconciliation_sweep), plus its on-demand entry points."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from aiogram.types import ChatMemberBanned, ChatMemberLeft, ChatMemberMember, Message, User

from database.models import DecisionEnum, Group, JoinRequest, ModerationLog, TelegramUser, UserBan
from moderation.actions import RECON_AUTO_CLOSE_NOTE, ReconciliationReport, run_reconciliation_sweep
import bot.handlers.ban_commands as ban_commands_module
from bot.handlers.ban_commands import (
    cb_reconcile_apply, cmd_reconcile, format_reconciliation_report,
)
from bot.handlers.settings_menu import cb_reconcile, cb_reconcile_apply_group, cb_reconcile_menu
import recovery.reconciliation_loop
from recovery.reconciliation_loop import _closed_count, run_daily_reconciliation
from recovery.sweep import run_recovery_sweep
from verification import VerificationEngine
from tests.conftest import make_tg_user, make_tg_chat, make_callback_query, drain_purge_tasks


@pytest.fixture(autouse=True)
def _clear_pending_reconciles():
    ban_commands_module._PENDING_RECONCILES.clear()
    yield
    ban_commands_module._PENDING_RECONCILES.clear()


def _member(user_id: int, status: str = "member") -> ChatMemberMember:
    return ChatMemberMember(status=status, user=User(id=user_id, is_bot=False, first_name="Dana"))


def _deleted_member() -> ChatMemberMember:
    user = User(id=0, is_bot=False, first_name="", username=None, last_name=None)
    return ChatMemberMember(status="member", user=user)


def _left_named(user_id: int) -> ChatMemberLeft:
    return ChatMemberLeft(status="left", user=User(id=user_id, is_bot=False, first_name="Dana"))


def _banned_named(user_id: int) -> ChatMemberBanned:
    return ChatMemberBanned(
        status="kicked", user=User(id=user_id, is_bot=False, first_name="Dana"), until_date=0,
    )


async def _open_row(db_session, group, user_id, decision=DecisionEnum.pending, created_at=None):
    db_session.add(TelegramUser(telegram_id=user_id, first_name="Dana"))
    jr = JoinRequest(
        group_id=group.id, user_id=user_id, chat_id=group.chat_id,
        decision=decision, score=10.0, matched_rules=[],
        created_at=created_at,
    )
    db_session.add(jr)
    await db_session.commit()
    await db_session.refresh(jr)
    return jr


def _missing_request_error() -> TelegramAPIError:
    return TelegramAPIError(method="declineChatJoinRequest", message="Bad Request: HIDE_REQUESTER_MISSING")


def _access_error() -> TelegramAPIError:
    return TelegramAPIError(method="getChatMember", message="Bad Request: chat not found")


# ─── Outcomes the sweep can prove ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_member_row_is_closed_as_joined(test_session_factory, sample_group, db_session, mock_bot):
    jr = await _open_row(db_session, sample_group, 9001)
    mock_bot.get_chat_member.return_value = _member(9001)

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.joined == [(sample_group.title, 9001)]
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.approved
        assert row.resolved_at is not None
        logs = (await check_db.execute(
            select(ModerationLog).where(ModerationLog.join_request_id == jr.id)
        )).scalars().all()
        assert len(logs) == 1
        assert logs[0].details.get("reconciliation") is True


@pytest.mark.asyncio
async def test_deactivated_account_is_closed_as_deleted(test_session_factory, sample_group, db_session, mock_bot):
    jr = await _open_row(db_session, sample_group, 9002)
    mock_bot.get_chat_member.return_value = _deleted_member()

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.deleted == [(sample_group.title, 9002)]
    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, 9002)
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_deactivated_account_whose_decline_is_refused_is_cleared_via_ban(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Reproduces the production failure: Telegram refuses decline with
    "Forbidden: user is deactivated" even though the request is still
    pending. The sweep must escalate to banChatMember and count the result
    honestly, apart from a clean decline."""
    jr = await _open_row(db_session, sample_group, 9048)
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.deleted == []
    assert report.deleted_via_ban == [(sample_group.title, 9048)]
    assert report.undetermined == 0
    mock_bot.ban_chat_member.assert_called_once_with(sample_group.chat_id, 9048)
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.cleared_via_ban
        assert row.resolved_at is not None

        ban_row = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == 9048, UserBan.lifted_at.is_(None))
        )).scalars().first()
        assert ban_row is not None
        assert ban_row.scope == "local"
        assert ban_row.group_id == sample_group.id


@pytest.mark.asyncio
async def test_deactivated_account_whose_decline_is_refused_for_a_non_deactivation_reason_never_bans(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Finding D: a rights error, a network error, or any other failure that
    is not the explicit "user is deactivated" refusal must never escalate to
    a ban - the row stays open and is reported as a clearing failure, not
    folded into the generic "undetermined" bucket."""
    jr = await _open_row(db_session, sample_group, 9050)
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: CHAT_ADMIN_REQUIRED"
    )

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.deleted_via_ban == []
    assert report.undetermined == 0
    assert len(report.deleted_failed) == 1
    mock_bot.ban_chat_member.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_unresolvable_account_never_poisons_the_stale_rule_or_notifies_daily(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    """Finding B: PARTICIPANT_ID_INVALID on both getChatMember and the
    getChat fallback means Telegram has no record of this user in this chat
    at all. It must be its own state, it must not disable the group's
    stale-close rule, and it must not make the daily loop notify the owner
    every single day forever just because this row exists."""
    from config import settings
    now = datetime.now(timezone.utc)
    unresolvable_jr = await _open_row(db_session, sample_group, 9051, created_at=now - timedelta(days=10))
    stale_jr = await _open_row(db_session, sample_group, 9052, created_at=now - timedelta(days=10))

    async def fake_get_chat_member(chat_id, user_id):
        if user_id == 9051:
            raise TelegramAPIError(method="getChatMember", message="Bad Request: PARTICIPANT_ID_INVALID")
        return _left_named(user_id)

    mock_bot.get_chat_member.side_effect = fake_get_chat_member
    mock_bot.get_chat.side_effect = TelegramAPIError(method="getChat", message="Bad Request: chat not found")

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert report.unresolvable == [(sample_group.title, 9051)]
    assert report.stale_skipped_groups == []
    assert [uid for _, uid in report.auto_closed_stale] == [9052]
    async with test_session_factory() as check_db:
        unresolvable_row = await check_db.get(JoinRequest, unresolvable_jr.id)
        assert unresolvable_row.resolved_at is None
        assert unresolvable_row.decision == DecisionEnum.pending

        stale_row = await check_db.get(JoinRequest, stale_jr.id)
        assert stale_row.decision == DecisionEnum.stale

    # The daily loop must not notify the owner just because an unresolvable
    # row exists: nothing closed, nothing to disclose via stale_skipped_groups.
    monkeypatch.setattr(settings, "super_admin_id", 1)
    only_unresolvable_jr = await _open_row(db_session, sample_group, 9053)
    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Bad Request: USER_ID_INVALID",
    )
    mock_bot.send_message.reset_mock()

    await run_daily_reconciliation(mock_bot)

    mock_bot.send_message.assert_not_called()


def test_closed_count_includes_ban_cleared_rows():
    """Finding A: the daily loop's own "did anything close" check used to
    ignore deleted_via_ban entirely, so a run that only closed rows that way
    stayed silent."""
    report = ReconciliationReport(deleted_via_ban=[("G", 1), ("G", 2)])
    assert _closed_count(report) == 2


def test_reconciliation_report_totals_equal_the_sum_of_their_parts():
    """Finding 5: the per-group "deleted" line used to silently absorb
    deleted_request_gone (double-counting against the separate top-level
    bucket), a clearing failure was folded into "undetermined" instead of
    its own line, and a race-lost row vanished instead of getting a line of
    its own. All four must now add up."""
    stats = {
        "title": "G", "checked": 4, "joined": 0, "deleted": 1,
        "deleted_request_gone": 1, "deleted_via_ban": 1, "deleted_failed": 0,
        "banned": 0, "auto_closed_stale": 0, "undetermined": 0,
        "unresolvable": 0, "race_lost": 1, "access_error": None,
    }
    report = ReconciliationReport(
        checked=4,
        deleted=[("G", 1)],
        deleted_request_gone=[("G", 2)],
        deleted_via_ban=[("G", 3)],
        race_lost=[("G", 4)],
        per_group={1: stats},
        member_counts={1: 100},
    )
    text = format_reconciliation_report(report, applied=True)

    assert len(report.deleted) == 1
    assert len(report.deleted_request_gone) == 1
    assert "הוכרע במקביל: 1" in text
    assert "ניקוי נכשל: 0" in text
    assert "כבר לא בטלגרם: 1" in text


def test_reconciliation_report_shows_ban_cleared_and_unresolvable_as_their_own_lines():
    report = ReconciliationReport(
        checked=5, deleted_via_ban=[("G", 1)], unresolvable=[("G", 2), ("G", 3)],
    )
    text = format_reconciliation_report(report, applied=True)

    assert "נוקה באמצעות חסימה: 1" in text
    assert "רק מי שנכנס לאפליקציה יכול לטפל בה: 2" in text
    assert "לא הוכרע" in text
    assert "—" not in text


@pytest.mark.asyncio
async def test_undetermined_row_is_never_auto_closed_by_age_with_the_default_config(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Finding C (policy change): the owner's answer for a week-old LIVE
    applicant's request that Telegram still holds was to leave it alone -
    keep the row open and report it as undetermined. The config default for
    reconciliation_stale_days is 0, so calling the sweep without an explicit
    override must never auto-close an old undetermined row."""
    now = datetime.now(timezone.utc)
    jr = await _open_row(db_session, sample_group, 9054, created_at=now - timedelta(days=365))
    mock_bot.get_chat_member.return_value = _left_named(9054)

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.auto_closed_stale == []
    assert report.undetermined == 1
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None
        assert row.decision == DecisionEnum.pending


@pytest.mark.asyncio
async def test_deactivated_account_stuck_when_both_decline_and_ban_fail(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _open_row(db_session, sample_group, 9049, created_at=datetime.now(timezone.utc))
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )
    mock_bot.ban_chat_member.side_effect = TelegramAPIError(
        method="banChatMember", message="Forbidden: user is deactivated"
    )

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert report.deleted == []
    assert report.deleted_via_ban == []
    assert report.undetermined == 0
    assert len(report.deleted_failed) == 1
    # A deleted account that could not be cleared poisons the stale rule for
    # its own group too - it must never be allowed to age into a silent
    # "auto_closed_stale" instead of staying visibly stuck.
    assert sample_group.title in report.stale_skipped_groups
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.pending
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_kicked_outside_the_bot_is_closed_as_banned(test_session_factory, sample_group, db_session, mock_bot):
    jr = await _open_row(db_session, sample_group, 9003)
    mock_bot.get_chat_member.return_value = _banned_named(9003)

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.banned == [(sample_group.title, 9003)]
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.banned
        assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_still_pending_applicant_is_left_open_and_never_declined(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr = await _open_row(db_session, sample_group, 9004)
    mock_bot.get_chat_member.return_value = _left_named(9004)

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.undetermined == 1
    assert report.joined == []
    assert report.deleted == []
    assert report.banned == []
    # Not just "no error raised" - the decline must never even be attempted
    # against a user whose request could still be genuinely pending.
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None
        assert row.decision == DecisionEnum.pending


@pytest.mark.asyncio
async def test_group_access_error_closes_nothing_in_that_group(
    test_session_factory, sample_group, db_session, mock_bot
):
    jr1 = await _open_row(db_session, sample_group, 9005)
    jr2 = await _open_row(db_session, sample_group, 9006)
    mock_bot.get_chat_member.side_effect = _access_error()

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.joined == []
    assert report.deleted == []
    assert report.banned == []
    assert report.auto_closed_stale == []
    assert len(report.access_errors) == 1
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        for jr in (jr1, jr2):
            row = await check_db.get(JoinRequest, jr.id)
            assert row.resolved_at is None
            assert row.decision == DecisionEnum.pending


# ─── Stale auto-close ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stale_rule_closes_only_undetermined_rows_older_than_the_window(
    test_session_factory, sample_group, db_session, mock_bot
):
    now = datetime.now(timezone.utc)
    old_jr = await _open_row(db_session, sample_group, 9007, created_at=now - timedelta(days=10))
    fresh_jr = await _open_row(db_session, sample_group, 9008, created_at=now - timedelta(days=1))
    mock_bot.get_chat_member.return_value = _left_named(0)  # same undetermined outcome for both

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert [uid for _, uid in report.auto_closed_stale] == [9007]
    assert report.joined == []
    assert report.deleted == []
    assert report.banned == []

    async with test_session_factory() as check_db:
        old_row = await check_db.get(JoinRequest, old_jr.id)
        assert old_row.decision == DecisionEnum.stale
        assert old_row.decision not in (DecisionEnum.approved, DecisionEnum.rejected)
        assert old_row.resolved_at is not None
        assert old_row.notes == RECON_AUTO_CLOSE_NOTE.format(days=7)

        fresh_row = await check_db.get(JoinRequest, fresh_jr.id)
        assert fresh_row.resolved_at is None
        assert fresh_row.decision == DecisionEnum.pending

        logs = (await check_db.execute(
            select(ModerationLog).where(ModerationLog.join_request_id == old_jr.id)
        )).scalars().all()
        assert len(logs) == 1
        assert logs[0].decision == DecisionEnum.stale
        assert logs[0].details.get("reason") == "auto_closed_stale"


@pytest.mark.asyncio
async def test_stale_window_zero_disables_auto_close(test_session_factory, sample_group, db_session, mock_bot):
    now = datetime.now(timezone.utc)
    old_jr = await _open_row(db_session, sample_group, 9009, created_at=now - timedelta(days=365))
    mock_bot.get_chat_member.return_value = _left_named(9009)

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=0, db=db_session)

    assert report.auto_closed_stale == []
    assert report.undetermined == 1
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, old_jr.id)
        assert row.resolved_at is None
        assert row.decision == DecisionEnum.pending


@pytest.mark.asyncio
async def test_stale_window_zero_never_reports_stale_skipped_groups(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch,
):
    """Finding 6: with the stale-close rule off (stale_days=0), a group with
    a permanent probe error must never show up in stale_skipped_groups - that
    field existing with the rule off is exactly what let the daily loop
    notify the owner forever about a group it can never fix (see
    recovery/reconciliation_loop.py's _closed_count gate)."""
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    jr = await _open_row(db_session, sample_group, 9060)
    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Forbidden: bot was kicked from the supergroup chat",
    )
    mock_bot.get_chat.side_effect = TelegramAPIError(method="getChat", message="Bad Request: network error")

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=0, db=db_session)

    assert report.stale_skipped_groups == []
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None

    mock_bot.send_message.reset_mock()
    await run_daily_reconciliation(mock_bot)
    mock_bot.send_message.assert_not_called()


@pytest.mark.asyncio
async def test_reconcile_menu_text_says_rule_is_off_instead_of_quoting_zero_days(
    sample_group, db_session, monkeypatch,
):
    """Finding 6: with stale_days=0 the menu used to claim rows auto-close
    "after 0 days have passed", which is false - the rule is off, and the
    text must say so plainly instead."""
    from config import settings
    monkeypatch.setattr(settings, "reconciliation_stale_days", 0)
    owner = make_tg_user(user_id=sample_group.owner_id, username="owner")

    cb = make_callback_query(f"sm:reconcile_menu:{sample_group.id}", user=owner)
    await cb_reconcile_menu(cb)

    text = cb.message.edit_text.call_args.args[0]
    assert "0 ימים" not in text
    assert "כבויה" in text


# ─── Dry run and idempotency ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_dry_run_closes_nothing_but_prints_the_full_breakdown(
    test_session_factory, sample_group, db_session, mock_bot
):
    now = datetime.now(timezone.utc)
    joined_jr = await _open_row(db_session, sample_group, 9010)
    deleted_jr = await _open_row(db_session, sample_group, 9011)
    stale_jr = await _open_row(db_session, sample_group, 9012, created_at=now - timedelta(days=30))

    async def fake_get_chat_member(chat_id, user_id):
        if user_id == 9010:
            return _member(9010)
        if user_id == 9011:
            return _deleted_member()
        return _left_named(user_id)

    mock_bot.get_chat_member.side_effect = fake_get_chat_member

    report = await run_reconciliation_sweep(mock_bot, dry_run=True, stale_days=7, db=db_session)

    mock_bot.decline_chat_join_request.assert_not_called()
    assert report.joined == [(sample_group.title, 9010)]
    assert report.deleted == [(sample_group.title, 9011)]
    assert report.auto_closed_stale == [(sample_group.title, 9012)]

    async with test_session_factory() as check_db:
        for jr in (joined_jr, deleted_jr, stale_jr):
            row = await check_db.get(JoinRequest, jr.id)
            assert row.resolved_at is None
            assert row.decision == DecisionEnum.pending

    text = format_reconciliation_report(report, applied=False)
    assert "בקשות פתוחות במסד הנתונים של הבוט: 3" in text
    assert "הצטרפו לקבוצה (אושרו מחוץ לבוט): 1" in text
    assert "חשבונות מחוקים שאותרו" in text
    assert "יסגרו אוטומטית בגלל גיל" in text
    assert "—" not in text


@pytest.mark.asyncio
async def test_rerunning_the_sweep_is_idempotent(test_session_factory, sample_group, db_session, mock_bot):
    await _open_row(db_session, sample_group, 9013)
    mock_bot.get_chat_member.return_value = _member(9013)

    first = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)
    assert first.checked == 1
    assert first.joined == [(sample_group.title, 9013)]

    async with test_session_factory() as db2:
        second = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db2)

    assert second.checked == 0
    assert second.joined == []
    assert mock_bot.decline_chat_join_request.call_count == 0


# ─── Entry points: answer first, never silent ─────────────────────────────────

def _reconcile_command(user_id: int = 1) -> Message:
    from unittest.mock import AsyncMock
    message = AsyncMock(spec=Message)
    message.text = "/reconcile"
    message.from_user = make_tg_user(user_id=user_id, username="superadmin")
    message.chat = make_tg_chat()
    message.reply = AsyncMock()
    return message


@pytest.mark.asyncio
async def test_reconcile_command_reports_and_offers_confirm_when_something_was_found(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    await _open_row(db_session, sample_group, 9014)
    mock_bot.get_chat_member.return_value = _member(9014)

    message = _reconcile_command()
    await cmd_reconcile(message, mock_bot)

    message.reply.assert_called_once()
    text = message.reply.call_args.args[0]
    assert "בדיקה בלבד, עדיין לא נסגר דבר" in text
    markup = message.reply.call_args.kwargs["reply_markup"]
    assert [b.callback_data for row in markup.inline_keyboard for b in row] == ["recon:apply:all"]
    assert ban_commands_module._PENDING_RECONCILES[1]["group_id"] is None
    # The dry run itself must never have closed anything.
    async with test_session_factory() as check_db:
        rows = (await check_db.execute(
            select(JoinRequest)
        )).scalars().all()
        assert all(r.resolved_at is None for r in rows)


@pytest.mark.asyncio
async def test_reconcile_command_with_nothing_to_act_on_offers_no_confirm(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    await _open_row(db_session, sample_group, 9015)
    mock_bot.get_chat_member.return_value = _left_named(9015)

    message = _reconcile_command()
    await cmd_reconcile(message, mock_bot)

    assert message.reply.call_args.kwargs["reply_markup"] is None
    assert "אין מה לסגור." in message.reply.call_args.args[0]
    assert 1 not in ban_commands_module._PENDING_RECONCILES


@pytest.mark.asyncio
async def test_reconcile_apply_callback_closes_only_the_pending_dry_run_and_only_once(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    jr = await _open_row(db_session, sample_group, 9016)
    mock_bot.get_chat_member.return_value = _deleted_member()

    message = _reconcile_command()
    await cmd_reconcile(message, mock_bot)

    apply_cb = make_callback_query("recon:apply:all", user=make_tg_user(user_id=1, username="superadmin"))
    await cb_reconcile_apply(apply_cb, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, 9016)
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None

    again = make_callback_query("recon:apply:all", user=make_tg_user(user_id=1, username="superadmin"))
    await cb_reconcile_apply(again, mock_bot)
    assert mock_bot.decline_chat_join_request.call_count == 1
    again.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_settings_reconcile_button_answers_immediately_and_confirm_runs_once(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    owner = make_tg_user(user_id=sample_group.owner_id, username="owner")
    jr = await _open_row(db_session, sample_group, 9017)
    mock_bot.get_chat_member.return_value = _deleted_member()

    first = make_callback_query(f"sm:reconcile:{sample_group.id}", user=owner)
    await cb_reconcile(first, mock_bot)

    # Answered before the sweep (which can outlive a callback's lifetime) even
    # starts probing - checked before draining the background task.
    first.answer.assert_called_once()
    mock_bot.decline_chat_join_request.assert_not_called()

    await drain_purge_tasks()

    markup = first.message.edit_text.call_args.kwargs["reply_markup"]
    assert f"sm:reconcile_apply:{sample_group.id}" in [
        b.callback_data for row in markup.inline_keyboard for b in row
    ]
    async with test_session_factory() as check_db:
        assert (await check_db.get(JoinRequest, jr.id)).resolved_at is None

    confirm = make_callback_query(f"sm:reconcile_apply:{sample_group.id}", user=owner)
    await cb_reconcile_apply_group(confirm, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, 9017)
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None

    # A second press on the same (already-consumed) confirm button is refused,
    # never leaving the admin stuck mid-sweep either.
    again = make_callback_query(f"sm:reconcile_apply:{sample_group.id}", user=owner)
    await cb_reconcile_apply_group(again, mock_bot)
    assert mock_bot.decline_chat_join_request.call_count == 1
    again.message.edit_text.assert_not_called()


# ─── A probe failure is never evidence of membership (review finding 1) ───────

@pytest.mark.asyncio
async def test_probe_network_error_never_closes_a_row_as_stale(
    test_session_factory, sample_group, db_session, mock_bot
):
    """A kicked-from-chat / network-style error on both the primary probe and
    its get_chat fallback must leave the row open, never prove "not a
    member", and mark the group as skipped for stale-closing."""
    now = datetime.now(timezone.utc)
    jr = await _open_row(db_session, sample_group, 9029, created_at=now - timedelta(days=10))

    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Forbidden: bot was kicked from the supergroup chat",
    )
    mock_bot.get_chat.side_effect = TelegramAPIError(
        method="getChat", message="Bad Request: network error",
    )

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert report.auto_closed_stale == []
    assert sample_group.title in report.stale_skipped_groups
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None
        assert row.decision == DecisionEnum.pending


@pytest.mark.asyncio
async def test_group_with_exhausted_retry_skips_stale_but_healthy_group_still_closes(
    test_session_factory, sample_group, db_session, mock_bot
):
    """An exhausted 429 on one group's probe must not block the exact same
    run's stale rule in a different, healthy group - the gate is per group,
    not global."""
    now = datetime.now(timezone.utc)
    other_group = Group(chat_id=-1009999999, title="Other Group", owner_id=55555, is_active=True)
    db_session.add(other_group)
    await db_session.commit()
    await db_session.refresh(other_group)

    error_jr = await _open_row(db_session, sample_group, 9030, created_at=now - timedelta(days=10))
    healthy_jr = await _open_row(db_session, other_group, 9031, created_at=now - timedelta(days=10))

    async def fake_get_chat_member(chat_id, user_id):
        if chat_id == sample_group.chat_id:
            # Exhausted 429: every attempt is throttled, so
            # _call_honouring_flood eventually re-raises the last one.
            raise TelegramRetryAfter(method="getChatMember", message="Too Many Requests", retry_after=0)
        return _left_named(user_id)

    mock_bot.get_chat_member.side_effect = fake_get_chat_member

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert [uid for _, uid in report.auto_closed_stale] == [9031]
    assert sample_group.title in report.stale_skipped_groups
    assert other_group.title not in report.stale_skipped_groups

    async with test_session_factory() as check_db:
        error_row = await check_db.get(JoinRequest, error_jr.id)
        assert error_row.resolved_at is None
        assert error_row.decision == DecisionEnum.pending

        healthy_row = await check_db.get(JoinRequest, healthy_jr.id)
        assert healthy_row.decision == DecisionEnum.stale
        assert healthy_row.resolved_at is not None


@pytest.mark.asyncio
async def test_a_real_not_a_member_status_still_closes_an_old_row_as_stale(
    test_session_factory, sample_group, db_session, mock_bot
):
    """A genuine "left" answer straight from getChatMember (no exception at
    all) is exactly the evidence the stale rule is allowed to act on."""
    now = datetime.now(timezone.utc)
    jr = await _open_row(db_session, sample_group, 9035, created_at=now - timedelta(days=30))
    mock_bot.get_chat_member.return_value = _left_named(9035)

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert [uid for _, uid in report.auto_closed_stale] == [9035]
    assert report.stale_skipped_groups == []
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.stale
        assert row.resolved_at is not None


@pytest.mark.asyncio
async def test_unknown_age_row_is_never_auto_closed_as_stale(
    test_session_factory, sample_group, db_session, mock_bot
):
    """created_at=None (a manual edit or an import) must be a deliberate
    exemption from the stale rule, never treated as infinitely old."""
    jr = await _open_row(db_session, sample_group, 9032)
    jr.created_at = None
    await db_session.commit()
    mock_bot.get_chat_member.return_value = _left_named(9032)

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert report.auto_closed_stale == []
    assert report.undetermined == 1
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None
        assert row.decision == DecisionEnum.pending


# ─── A decision taken mid-sweep always wins (review finding 3) ────────────────

@pytest.mark.asyncio
async def test_approval_landed_before_the_sweep_started_is_never_touched(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Ordering 1: the row is already closed by the time the sweep's phase 1
    query runs, so it is never even probed."""
    now = datetime.now(timezone.utc)
    jr = await _open_row(db_session, sample_group, 9036, created_at=now - timedelta(days=10))
    jr.decision = DecisionEnum.approved
    jr.resolved_at = now
    await db_session.commit()

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert report.checked == 0
    mock_bot.get_chat_member.assert_not_called()
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.approved


@pytest.mark.asyncio
async def test_approval_during_probe_is_not_overwritten_by_stale_close(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Ordering 2: the row is still open when phase 1 loads it, but an admin
    approves it (via a separate session, exactly like the settings-menu
    handler) while this sweep's probe for the same row is in flight. The
    conditional close must lose the race and never overwrite the approval."""
    now = datetime.now(timezone.utc)
    jr = await _open_row(db_session, sample_group, 9033, created_at=now - timedelta(days=10))

    async def fake_get_chat_member(chat_id, user_id):
        async with test_session_factory() as other_db:
            row = await other_db.get(JoinRequest, jr.id)
            row.decision = DecisionEnum.approved
            row.resolved_at = datetime.now(timezone.utc)
            row.reviewed_by = "owner"
            await other_db.commit()
        return _left_named(user_id)

    mock_bot.get_chat_member.side_effect = fake_get_chat_member

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert report.auto_closed_stale == []
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.approved
        assert row.resolved_at is not None

        logs = (await check_db.execute(
            select(ModerationLog).where(ModerationLog.join_request_id == jr.id)
        )).scalars().all()
        assert len(logs) == 0  # the sweep's own write lost the race and logged nothing


@pytest.mark.asyncio
async def test_concurrently_resolved_deleted_account_is_not_closed_or_logged_twice(
    test_session_factory, sample_group, db_session, mock_bot
):
    """A row resolved by another process while the "deleted account" probe is
    in flight must end up closed, and logged, exactly once."""
    jr = await _open_row(db_session, sample_group, 9034)

    async def fake_get_chat_member(chat_id, user_id):
        async with test_session_factory() as other_db:
            row = await other_db.get(JoinRequest, jr.id)
            row.decision = DecisionEnum.approved
            row.resolved_at = datetime.now(timezone.utc)
            other_db.add(ModerationLog(
                group_id=row.group_id, join_request_id=row.id, user_id=row.user_id,
                decision=DecisionEnum.approved, score=row.score, matched_rules=row.matched_rules,
                details={"manual_review": True},
            ))
            await other_db.commit()
        return _deleted_member()

    mock_bot.get_chat_member.side_effect = fake_get_chat_member

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, db=db_session)

    assert report.deleted == []
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.approved

        logs = (await check_db.execute(
            select(ModerationLog).where(ModerationLog.join_request_id == jr.id)
        )).scalars().all()
        assert len(logs) == 1


# ─── The global confirm button is bound to its scope (review finding 4) ───────

@pytest.mark.asyncio
async def test_reconcile_apply_refuses_scope_mismatch(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    jr = await _open_row(db_session, sample_group, 9024)
    mock_bot.get_chat_member.return_value = _deleted_member()

    message = _reconcile_command()
    await cmd_reconcile(message, mock_bot)  # stores pending group_id=None, scope "all"

    mismatched = make_callback_query(
        f"recon:apply:{sample_group.id}", user=make_tg_user(user_id=1, username="superadmin"),
    )
    await cb_reconcile_apply(mismatched, mock_bot)

    mismatched.answer.assert_called_once()
    assert mismatched.answer.call_args.args[0] == (
        "הכפתור הזה שייך לבדיקה אחרת מזו שמוצגת כרגע. יש להריץ בדיקה חדשה וללחוץ על הכפתור שלה."
    )
    mock_bot.decline_chat_join_request.assert_not_called()
    # One-shot: the pending record is consumed even though the button was refused.
    assert 1 not in ban_commands_module._PENDING_RECONCILES
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None


@pytest.mark.asyncio
async def test_reconcile_apply_refuses_the_legacy_bare_token(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    jr = await _open_row(db_session, sample_group, 9025)
    mock_bot.get_chat_member.return_value = _deleted_member()

    message = _reconcile_command()
    await cmd_reconcile(message, mock_bot)

    legacy = make_callback_query("recon:apply", user=make_tg_user(user_id=1, username="superadmin"))
    await cb_reconcile_apply(legacy, mock_bot)

    legacy.answer.assert_called_once()
    assert "ישן" in legacy.answer.call_args.args[0]
    mock_bot.decline_chat_join_request.assert_not_called()
    # A malformed/legacy token is refused before the pending record is even
    # touched, so a genuinely matching button can still be pressed afterward.
    assert ban_commands_module._PENDING_RECONCILES[1]["group_id"] is None
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.resolved_at is None


# ─── Recovery really runs the reconciliation sweep once (review finding 2) ────

@pytest.mark.asyncio
async def test_recovery_sweep_triggers_the_reconciliation_sweep_once_after_an_outage(
    test_session_factory, mock_bot, monkeypatch
):
    calls = []

    async def fake_run_daily_reconciliation(bot):
        calls.append(bot)

    monkeypatch.setattr(
        recovery.reconciliation_loop, "run_daily_reconciliation", fake_run_daily_reconciliation,
    )

    event = asyncio.Event()
    outage_started_at = datetime.now(timezone.utc) - timedelta(minutes=10)
    await run_recovery_sweep(mock_bot, VerificationEngine(), outage_started_at, 600.0, 0, event)

    assert event.is_set()
    assert calls == [mock_bot]


@pytest.mark.asyncio
async def test_recovery_sweep_triggers_reconciliation_even_without_an_outage(
    test_session_factory, mock_bot, monkeypatch
):
    calls = []

    async def fake_run_daily_reconciliation(bot):
        calls.append(bot)

    monkeypatch.setattr(
        recovery.reconciliation_loop, "run_daily_reconciliation", fake_run_daily_reconciliation,
    )

    event = asyncio.Event()
    await run_recovery_sweep(mock_bot, VerificationEngine(), None, 0.0, 0, event)

    assert event.is_set()
    assert calls == [mock_bot]


# ─── The per-group probe-error lock is itself tested (review finding 3) ───────

@pytest.mark.asyncio
async def test_error_row_blocks_stale_close_for_another_row_in_the_same_group(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Deleting the `g.id not in groups_with_errors` guard would not fail any
    existing test: they only cover a single error row (which skips itself
    anyway) or an error in a different group. Here one row's probe fails and
    a second, unrelated old row in the SAME group would otherwise cleanly
    qualify for the stale rule - it must stay open too."""
    now = datetime.now(timezone.utc)
    error_jr = await _open_row(db_session, sample_group, 9041, created_at=now - timedelta(days=10))
    old_jr = await _open_row(db_session, sample_group, 9042, created_at=now - timedelta(days=10))

    async def fake_get_chat_member(chat_id, user_id):
        if user_id == 9041:
            raise TelegramAPIError(
                method="getChatMember", message="Forbidden: bot was kicked from the supergroup chat",
            )
        return _left_named(user_id)

    mock_bot.get_chat_member.side_effect = fake_get_chat_member
    mock_bot.get_chat.side_effect = TelegramAPIError(method="getChat", message="Bad Request: network error")

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert report.auto_closed_stale == []
    assert sample_group.title in report.stale_skipped_groups
    async with test_session_factory() as check_db:
        old_row = await check_db.get(JoinRequest, old_jr.id)
        assert old_row.resolved_at is None
        assert old_row.decision == DecisionEnum.pending

        error_row = await check_db.get(JoinRequest, error_jr.id)
        assert error_row.resolved_at is None


@pytest.mark.asyncio
async def test_a_different_healthy_group_still_closes_its_stale_row_in_the_same_run(
    test_session_factory, sample_group, db_session, mock_bot
):
    """The same run as above, but with a second, healthy group added: that
    group's own old row must still close as stale - the error-row guard is
    per group, not a global kill switch for the whole sweep."""
    now = datetime.now(timezone.utc)
    other_group = Group(chat_id=-1008888888, title="Second Group", owner_id=44444, is_active=True)
    db_session.add(other_group)
    await db_session.commit()
    await db_session.refresh(other_group)

    await _open_row(db_session, sample_group, 9043, created_at=now - timedelta(days=10))
    await _open_row(db_session, sample_group, 9044, created_at=now - timedelta(days=10))
    healthy_jr = await _open_row(db_session, other_group, 9045, created_at=now - timedelta(days=10))

    async def fake_get_chat_member(chat_id, user_id):
        if chat_id == sample_group.chat_id and user_id == 9043:
            raise TelegramAPIError(
                method="getChatMember", message="Forbidden: bot was kicked from the supergroup chat",
            )
        return _left_named(user_id)

    mock_bot.get_chat_member.side_effect = fake_get_chat_member
    mock_bot.get_chat.side_effect = TelegramAPIError(method="getChat", message="Bad Request: network error")

    report = await run_reconciliation_sweep(mock_bot, dry_run=False, stale_days=7, db=db_session)

    assert [uid for _, uid in report.auto_closed_stale] == [9045]
    assert sample_group.title in report.stale_skipped_groups
    assert other_group.title not in report.stale_skipped_groups
    async with test_session_factory() as check_db:
        healthy_row = await check_db.get(JoinRequest, healthy_jr.id)
        assert healthy_row.decision == DecisionEnum.stale
        assert healthy_row.resolved_at is not None


# ─── Two reconciliation sweeps can never overlap in-process (review finding 2) ─

@pytest.mark.asyncio
async def test_concurrent_daily_reconciliation_sweeps_cannot_both_run(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    """A second call to run_daily_reconciliation that arrives while the first
    is still mid-probe must not run a sweep of its own - it must skip, not
    race the first one. Verified through real behaviour: the row ends up
    closed exactly once, never declined twice."""
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    jr = await _open_row(db_session, sample_group, 9046)

    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_get_chat_member(chat_id, user_id):
        started.set()
        await release.wait()
        return _deleted_member()

    mock_bot.get_chat_member.side_effect = slow_get_chat_member

    first_task = asyncio.create_task(recovery.reconciliation_loop.run_daily_reconciliation(mock_bot))
    await started.wait()  # the first sweep now holds the lock, mid-probe

    # A second, concurrent attempt must skip outright, never probing at all.
    await recovery.reconciliation_loop.run_daily_reconciliation(mock_bot)
    assert mock_bot.decline_chat_join_request.call_count == 0

    release.set()
    await first_task

    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, 9046)
    async with test_session_factory() as check_db:
        row = await check_db.get(JoinRequest, jr.id)
        assert row.decision == DecisionEnum.rejected
        assert row.resolved_at is not None
