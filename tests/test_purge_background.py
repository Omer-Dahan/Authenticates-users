"""Regression tests for the purge button that "did nothing": the sweep ran inline
in the callback handler for minutes, the callback query expired, and the late
`callback.answer` raised before the result was ever shown. The handlers now
answer first, run the sweep as a background task, and probe in parallel."""
import asyncio
import time

import pytest
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, DeclineChatJoinRequest, GetChatMember
from aiogram.types import User, ChatMemberLeft, ChatMemberMember

from database.models import DecisionEnum, Group, JoinRequest
from moderation.actions import purge_deleted_requests
import bot.handlers.ban_commands as ban_commands_module
import bot.handlers.settings_menu as settings_menu_module
import bot.handlers.superadmin as superadmin_module
from bot.handlers.ban_commands import cb_purge_apply
from bot.handlers.settings_menu import cb_purge_menu, cb_purge_apply_group
from bot.handlers.superadmin import cb_purge_prompt
from tests.conftest import make_tg_user, make_callback_query, drain_purge_tasks


@pytest.fixture
async def second_group(db_session):
    group = Group(
        chat_id=-1009876543210,
        title="Second Community",
        username="second_comm",
        owner_id=555555555,
        is_active=True,
    )
    db_session.add(group)
    await db_session.commit()
    await db_session.refresh(group)
    return group


@pytest.fixture(autouse=True)
def _clean_purge_state(monkeypatch):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)
    ban_commands_module._PENDING_PURGES.clear()
    ban_commands_module._PURGE_RUNNING.clear()
    yield
    ban_commands_module._PENDING_PURGES.clear()
    ban_commands_module._PURGE_RUNNING.clear()


def _admin() -> User:
    return make_tg_user(user_id=1, username="superadmin")


def _deleted_member() -> ChatMemberMember:
    return ChatMemberMember(status="member", user=User(id=0, is_bot=False, first_name=""))


def _left_named(user_id: int) -> ChatMemberLeft:
    return ChatMemberLeft(status="left", user=User(id=user_id, is_bot=False, first_name="Dana"))


async def _open_rows(db_session, group, user_ids):
    rows = []
    for user_id in user_ids:
        jr = JoinRequest(
            group_id=group.id, user_id=user_id, chat_id=group.chat_id,
            decision=DecisionEnum.pending, score=0.0, matched_rules=[],
        )
        db_session.add(jr)
        rows.append(jr)
    await db_session.commit()
    return rows


def _last_edit_text(callback) -> str:
    return callback.message.edit_text.call_args.args[0]


# ─── Answer first, sweep later ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_apply_answers_the_callback_before_any_probe(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9001])
    order = []

    async def answer(*args, **kwargs):
        order.append("answer")

    async def probe(chat_id, user_id):
        order.append("probe")
        return _deleted_member()

    mock_bot.get_chat_member.side_effect = probe
    ban_commands_module._store_pending_purge(1, sample_group.id)
    callback = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())
    callback.answer.side_effect = answer

    await cb_purge_apply(callback, mock_bot)
    await drain_purge_tasks()

    assert order[0] == "answer"
    assert order.count("answer") == 1
    assert "probe" in order
    assert "הסרה בוצעה" in _last_edit_text(callback)


@pytest.mark.asyncio
async def test_settings_dry_run_answers_the_callback_before_any_probe(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9002])
    order = []

    async def answer(*args, **kwargs):
        order.append("answer")

    async def probe(chat_id, user_id):
        order.append("probe")
        return _deleted_member()

    mock_bot.get_chat_member.side_effect = probe
    owner = make_tg_user(user_id=sample_group.owner_id, username="owner")
    callback = make_callback_query(f"sm:purge:{sample_group.id}", user=owner)
    callback.answer.side_effect = answer

    await cb_purge_menu(callback, mock_bot)
    await drain_purge_tasks()

    assert order[0] == "answer"
    assert "probe" in order
    assert "בדיקה בלבד, עדיין לא הוסר דבר." in _last_edit_text(callback)


@pytest.mark.asyncio
async def test_handler_returns_at_once_and_the_sweep_runs_as_a_task(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9003])
    release = asyncio.Event()

    async def slow_probe(chat_id, user_id):
        await release.wait()
        return _deleted_member()

    mock_bot.get_chat_member.side_effect = slow_probe
    ban_commands_module._store_pending_purge(1, sample_group.id)
    callback = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())

    await asyncio.wait_for(cb_purge_apply(callback, mock_bot), timeout=1)

    tasks = list(ban_commands_module._PURGE_TASKS)
    assert len(tasks) == 1 and not tasks[0].done()
    callback.answer.assert_awaited_once()
    mock_bot.decline_chat_join_request.assert_not_called()

    # A second press while the sweep runs is refused instead of starting another.
    ban_commands_module._store_pending_purge(1, sample_group.id)
    again = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())
    await cb_purge_apply(again, mock_bot)
    assert len(ban_commands_module._PURGE_TASKS) == 1
    assert "עדיין רצה" in again.answer.call_args.args[0]

    release.set()
    await drain_purge_tasks()
    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, 9003)
    assert "הסרה בוצעה" in _last_edit_text(callback)
    assert not ban_commands_module.purge_in_progress(1)


@pytest.mark.asyncio
async def test_an_expired_callback_query_does_not_hide_the_result(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9004])
    mock_bot.get_chat_member.return_value = _deleted_member()
    ban_commands_module._store_pending_purge(1, sample_group.id)
    callback = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())
    callback.answer.side_effect = TelegramBadRequest(
        method=AnswerCallbackQuery(callback_query_id="1"),
        message="Bad Request: query is too old and response timeout expired or query ID is invalid",
    )

    await cb_purge_apply(callback, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_called_once()
    assert "הסרה בוצעה" in _last_edit_text(callback)


# ─── Never silence ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_failing_apply_sweep_still_edits_a_failure_line(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ban_commands_module, "purge_deleted_requests", boom)
    ban_commands_module._store_pending_purge(1, sample_group.id)
    callback = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())

    await cb_purge_apply(callback, mock_bot)
    await drain_purge_tasks()

    assert "❌ הבדיקה נכשלה" in _last_edit_text(callback)
    assert not ban_commands_module.purge_in_progress(1)


@pytest.mark.asyncio
async def test_a_failing_settings_sweep_still_edits_a_failure_line(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(settings_menu_module, "purge_deleted_requests", boom)
    owner = make_tg_user(user_id=sample_group.owner_id, username="owner")

    dry = make_callback_query(f"sm:purge:{sample_group.id}", user=owner)
    await cb_purge_menu(dry, mock_bot)
    await drain_purge_tasks()
    assert "❌ הבדיקה נכשלה" in _last_edit_text(dry)
    assert sample_group.id not in [e["group_id"] for e in ban_commands_module._PENDING_PURGES.values()]

    ban_commands_module._store_pending_purge(owner.id, sample_group.id)
    confirm = make_callback_query(f"sm:purge_apply:{sample_group.id}", user=owner)
    await cb_purge_apply_group(confirm, mock_bot)
    await drain_purge_tasks()
    assert "❌ הבדיקה נכשלה" in _last_edit_text(confirm)


# ─── Parallel probing ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probing_respects_the_concurrency_limit(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, range(9100, 9130))
    in_flight = 0
    peak = 0

    async def counting_probe(chat_id, user_id):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return _left_named(user_id)

    mock_bot.get_chat_member.side_effect = counting_probe

    report = await purge_deleted_requests(mock_bot, dry_run=True, db=db_session, concurrency=5)

    assert report.checked == 30
    assert report.alive == 30
    assert peak == 5


@pytest.mark.asyncio
async def test_progress_is_throttled(test_session_factory, sample_group, db_session, mock_bot):
    await _open_rows(db_session, sample_group, range(9200, 9210))
    mock_bot.get_chat_member.side_effect = lambda chat_id, user_id: _left_named(user_id)
    calls = []

    async def progress(done, total):
        calls.append((done, total))

    await purge_deleted_requests(mock_bot, dry_run=True, db=db_session, progress=progress, progress_interval=60)
    assert calls == []

    await purge_deleted_requests(mock_bot, dry_run=True, db=db_session, progress=progress, progress_interval=0)
    assert len(calls) == 10 and calls[-1] == (10, 10)


@pytest.mark.asyncio
async def test_a_429_on_the_probe_is_waited_out_and_does_not_fail_or_close_the_row(
    test_session_factory, sample_group, db_session, mock_bot
):
    (jr,) = await _open_rows(db_session, sample_group, [9300])
    mock_bot.get_chat_member.side_effect = [
        TelegramRetryAfter(method=GetChatMember(chat_id=sample_group.chat_id, user_id=9300),
                           message="Too Many Requests", retry_after=1),
        _left_named(9300),
    ]

    started = time.monotonic()
    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert time.monotonic() - started >= 1
    assert mock_bot.get_chat_member.call_count == 2
    assert report.alive == 1
    assert report.inconclusive == 0
    assert report.decline_failed == []
    mock_bot.decline_chat_join_request.assert_not_called()
    async with test_session_factory() as check_db:
        assert (await check_db.get(JoinRequest, jr.id)).resolved_at is None


@pytest.mark.asyncio
async def test_a_429_on_the_decline_is_retried_instead_of_failing_the_row(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9301])
    mock_bot.get_chat_member.return_value = _deleted_member()
    mock_bot.decline_chat_join_request.side_effect = [
        TelegramRetryAfter(method=DeclineChatJoinRequest(chat_id=sample_group.chat_id, user_id=9301),
                           message="Too Many Requests", retry_after=0),
        True,
    ]

    report = await purge_deleted_requests(mock_bot, dry_run=False, db=db_session)

    assert mock_bot.decline_chat_join_request.call_count == 2
    assert report.decline_failed == []
    assert report.removed == [(sample_group.title, 9301)]


# ─── The scope travels with the button (CONFIRMED 1) ──────────────────────────
# cb_purge_apply is shared by /purge_deleted (per-group or global) and the
# super-admin panel (always global). Both used to emit the same bare
# "purge:apply" and trust whatever sits in the single per-admin pending slot,
# so a later dry run of a different scope could silently redirect an earlier
# button's press onto the wrong set of groups.

@pytest.mark.asyncio
async def test_stale_group_button_is_refused_after_a_later_global_dry_run(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9010])
    mock_bot.get_chat_member.return_value = _deleted_member()

    ban_commands_module._store_pending_purge(1, sample_group.id)   # per-group dry run shown
    ban_commands_module._store_pending_purge(1, None)               # later global dry run overwrites it

    stale_button = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())
    await cb_purge_apply(stale_button, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_not_called()
    answer_text = stale_button.answer.call_args.args[0]
    assert "בדיקה אחרת" in answer_text
    stale_button.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_stale_global_button_is_refused_after_a_later_group_dry_run(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9011])
    mock_bot.get_chat_member.return_value = _deleted_member()

    ban_commands_module._store_pending_purge(1, None)               # global dry run shown
    ban_commands_module._store_pending_purge(1, sample_group.id)    # later per-group dry run overwrites it

    stale_button = make_callback_query("purge:apply:all", user=_admin())
    await cb_purge_apply(stale_button, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_not_called()
    answer_text = stale_button.answer.call_args.args[0]
    assert "בדיקה אחרת" in answer_text
    stale_button.message.edit_text.assert_not_called()


@pytest.mark.asyncio
async def test_matching_global_button_still_performs_the_global_removal(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9012])
    mock_bot.get_chat_member.return_value = _deleted_member()
    ban_commands_module._store_pending_purge(1, None)

    callback = make_callback_query("purge:apply:all", user=_admin())
    await cb_purge_apply(callback, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_called_once_with(sample_group.chat_id, 9012)
    assert "הסרה בוצעה" in _last_edit_text(callback)


@pytest.mark.asyncio
async def test_a_legacy_bare_button_is_refused_cleanly_not_crashed(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9013])
    mock_bot.get_chat_member.return_value = _deleted_member()
    ban_commands_module._store_pending_purge(1, sample_group.id)

    old_message_button = make_callback_query("purge:apply", user=_admin())
    await cb_purge_apply(old_message_button, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_not_called()
    old_message_button.message.edit_text.assert_not_called()


# ─── No confirm button, no pending record, on an empty super-admin sweep
#     (CONFIRMED 5) ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_superadmin_dry_run_with_nothing_to_remove_shows_no_button_and_stores_no_pending(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9020])
    mock_bot.get_chat_member.side_effect = lambda chat_id, user_id: _left_named(user_id)

    callback = make_callback_query("sa:purge_prompt", user=_admin())
    await cb_purge_prompt(callback, mock_bot)
    await drain_purge_tasks()

    markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert not any(data.startswith("purge:apply") for data in callback_datas)
    assert 1 not in ban_commands_module._PENDING_PURGES
    assert "אין מה להסיר" in _last_edit_text(callback)


@pytest.mark.asyncio
async def test_superadmin_dry_run_with_nothing_to_remove_clears_a_stale_pending_record(
    test_session_factory, sample_group, db_session, mock_bot
):
    """Unlike the test above, a pending record already exists here (from an
    earlier dry run that *did* find something). The fixture's autouse clear
    would hide a missing cleanup call, so the record is seeded here, inside
    the test, after that clear already ran."""
    await _open_rows(db_session, sample_group, [9022])
    mock_bot.get_chat_member.side_effect = lambda chat_id, user_id: _left_named(user_id)
    ban_commands_module._store_pending_purge(1, sample_group.id)

    callback = make_callback_query("sa:purge_prompt", user=_admin())
    await cb_purge_prompt(callback, mock_bot)
    await drain_purge_tasks()

    assert 1 not in ban_commands_module._PENDING_PURGES


@pytest.mark.asyncio
async def test_superadmin_dry_run_with_something_to_remove_still_shows_the_button(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9021])
    mock_bot.get_chat_member.return_value = _deleted_member()

    callback = make_callback_query("sa:purge_prompt", user=_admin())
    await cb_purge_prompt(callback, mock_bot)
    await drain_purge_tasks()

    markup = callback.message.edit_text.call_args.kwargs["reply_markup"]
    callback_datas = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "purge:apply:all" in callback_datas
    assert ban_commands_module._PENDING_PURGES[1]["group_id"] is None


# ─── No path ends in silence (CONFIRMED 4) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_when_the_sweep_and_the_failure_edit_both_fail_a_new_message_is_sent(
    test_session_factory, sample_group, db_session, mock_bot, monkeypatch
):
    async def boom(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(ban_commands_module, "purge_deleted_requests", boom)
    ban_commands_module._store_pending_purge(1, sample_group.id)
    callback = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())
    callback.message.edit_text.side_effect = TelegramAPIError(
        method="editMessageText", message="Bad Request: message to edit not found",
    )

    await cb_purge_apply(callback, mock_bot)
    await drain_purge_tasks()

    callback.message.answer.assert_awaited()
    sent_text = callback.message.answer.call_args.args[0]
    assert "❌ הבדיקה נכשלה" in sent_text


@pytest.mark.asyncio
async def test_when_the_success_edit_fails_a_new_message_carries_the_full_report(
    test_session_factory, sample_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9014])
    mock_bot.get_chat_member.return_value = _deleted_member()
    ban_commands_module._store_pending_purge(1, sample_group.id)
    callback = make_callback_query(f"purge:apply:{sample_group.id}", user=_admin())
    callback.message.edit_text.side_effect = TelegramAPIError(
        method="editMessageText", message="Bad Request: message to edit not found",
    )

    await cb_purge_apply(callback, mock_bot)
    await drain_purge_tasks()

    callback.message.answer.assert_awaited()
    sent_text = callback.message.answer.call_args.args[0]
    assert "הסרה בוצעה" in sent_text


# ─── End-to-end: an older dry run's button loses to a newer dry run in the
#     other scope, through the real handlers (not a direct pending-record
#     write) ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_older_group_dry_run_button_is_refused_after_a_newer_global_dry_run_end_to_end(
    test_session_factory, sample_group, second_group, db_session, mock_bot
):
    await _open_rows(db_session, sample_group, [9060])
    await _open_rows(db_session, second_group, [9061])
    mock_bot.get_chat_member.return_value = _deleted_member()
    admin = _admin()

    # 1. Older dry run, scoped to one group, through the real settings-menu
    #    handler - this is the dry run the admin actually saw and whose
    #    button they are about to press.
    group_dry_run = make_callback_query(f"sm:purge:{sample_group.id}", user=admin)
    await cb_purge_menu(group_dry_run, mock_bot)
    await drain_purge_tasks()
    group_markup = group_dry_run.message.edit_text.call_args.kwargs["reply_markup"]
    old_button_data = next(
        b.callback_data for row in group_markup.inline_keyboard for b in row
        if b.callback_data.startswith("sm:purge_apply:")
    )

    # 2. A newer dry run in the other scope (global, via the super-admin
    #    panel) overwrites the single per-admin pending slot before the
    #    admin gets back to the first button.
    global_dry_run = make_callback_query("sa:purge_prompt", user=admin)
    await cb_purge_prompt(global_dry_run, mock_bot)
    await drain_purge_tasks()

    # 3. Pressing the older, now-stale button must refuse and perform no
    #    real removal, and must tell the admin why.
    stale_button = make_callback_query(old_button_data, user=admin)
    await cb_purge_apply_group(stale_button, mock_bot)
    await drain_purge_tasks()

    mock_bot.decline_chat_join_request.assert_not_called()
    answer_text = stale_button.answer.call_args.args[0]
    assert "פגה" in answer_text
    stale_button.message.edit_text.assert_not_called()
