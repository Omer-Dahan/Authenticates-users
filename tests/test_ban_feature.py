"""Regression tests for the manual user-ban feature (docs/BAN.md)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message

from config import settings
from database.models import (
    Group, JoinRequest, DecisionEnum, ModerationLog, VerificationSession,
    TelegramUser, UserBan,
)
from moderation import ModerationEngine
from moderation.actions import ban_user, unban_user
from bot.handlers.settings_menu import cb_manual_review
from bot.handlers.ban_commands import cb_ban_scope, cb_ban_lift, cmd_ban, cmd_unban
from tests.conftest import make_tg_user, make_tg_chat, make_callback_query


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


# ─── 1. Engine respects ban scope ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_engine_global_ban_blocks_every_group(sample_group, second_group, db_session):
    engine = ModerationEngine()
    user_data = {"user_id": 55001, "first_name": "Banned", "last_name": "User", "username": "banneduser"}

    db_session.add(UserBan(telegram_id=55001, scope="global", group_id=None))
    await db_session.commit()

    result1 = await engine.evaluate(user_data, db_session, group_id=sample_group.id)
    result2 = await engine.evaluate(user_data, db_session, group_id=second_group.id)

    assert result1.decision == DecisionEnum.banned
    assert result2.decision == DecisionEnum.banned


@pytest.mark.asyncio
async def test_engine_local_ban_blocks_only_its_own_group(sample_group, second_group, db_session):
    engine = ModerationEngine()
    user_data = {"user_id": 55002, "first_name": "Local", "last_name": "Banned", "username": "localbanned"}

    db_session.add(UserBan(telegram_id=55002, scope="local", group_id=sample_group.id))
    await db_session.commit()

    result_own = await engine.evaluate(user_data, db_session, group_id=sample_group.id)
    result_other = await engine.evaluate(user_data, db_session, group_id=second_group.id)

    assert result_own.decision == DecisionEnum.banned
    assert result_other.decision != DecisionEnum.banned


@pytest.mark.asyncio
async def test_engine_unban_lifts_the_block(sample_group, db_session):
    engine = ModerationEngine()
    user_data = {"user_id": 55003, "first_name": "Temp", "last_name": "Banned", "username": "tempbanned"}

    ban = UserBan(telegram_id=55003, scope="local", group_id=sample_group.id)
    db_session.add(ban)
    await db_session.commit()

    result_before = await engine.evaluate(user_data, db_session, group_id=sample_group.id)
    assert result_before.decision == DecisionEnum.banned

    ban.lifted_at = datetime.now(timezone.utc)
    await db_session.commit()

    result_after = await engine.evaluate(user_data, db_session, group_id=sample_group.id)
    assert result_after.decision != DecisionEnum.banned


# ─── 2. Service: scope global bans everywhere, closes open items ──────────────

@pytest.mark.asyncio
async def test_service_global_scope_bans_everywhere_and_closes_open_items(
    test_session_factory, sample_group, second_group, db_session, mock_bot
):
    user_id = 66001
    db_session.add(TelegramUser(telegram_id=user_id, username="globalban", first_name="Global"))
    jr = JoinRequest(
        group_id=sample_group.id, user_id=user_id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.pending, score=10.0, matched_rules=[],
    )
    db_session.add(jr)
    session = VerificationSession(user_id=user_id, chat_id=sample_group.chat_id, completed_at=None)
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(jr)
    await db_session.refresh(session)

    report = await ban_user(
        mock_bot, user_id, "global",
        reason="spam", actor_id=999, actor_name="admin", db=db_session,
    )

    assert sample_group.title in report.banned_in
    assert second_group.title in report.banned_in
    assert mock_bot.ban_chat_member.call_count == 2

    async with test_session_factory() as check_db:
        refreshed_jr = await check_db.get(JoinRequest, jr.id)
        assert refreshed_jr.decision == DecisionEnum.banned
        assert refreshed_jr.resolved_at is not None

        refreshed_session = await check_db.get(VerificationSession, session.id)
        assert refreshed_session.completed_at is not None
        assert refreshed_session.passed is False

        logs = (await check_db.execute(
            select(ModerationLog).where(ModerationLog.user_id == user_id)
        )).scalars().all()
        assert len(logs) == 1
        assert logs[0].details.get("manual_ban") is True

        ban_row = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == user_id)
        )).scalars().first()
        assert ban_row.scope == "global"
        assert ban_row.lifted_at is None


# ─── 3. Service: one failing chat does not abort the rest ─────────────────────

@pytest.mark.asyncio
async def test_service_one_failing_chat_does_not_abort_others(
    sample_group, second_group, db_session, mock_bot
):
    user_id = 66002

    async def ban_side_effect(chat_id, uid):
        if chat_id == sample_group.chat_id:
            raise TelegramAPIError(method="banChatMember", message="bot is not an admin")
        return True

    mock_bot.ban_chat_member.side_effect = ban_side_effect

    report = await ban_user(mock_bot, user_id, "global", actor_id=999, actor_name="admin", db=db_session)

    assert any(title == sample_group.title for title, _err in report.failed)
    assert second_group.title in report.banned_in
    assert mock_bot.ban_chat_member.call_count == 2


# ─── 4. Service: scope global_remove removes everywhere, blocks only origin ──

@pytest.mark.asyncio
async def test_service_global_remove_scope(sample_group, second_group, db_session, mock_bot):
    user_id = 66003
    engine = ModerationEngine()

    report = await ban_user(
        mock_bot, user_id, "global_remove",
        group_id=sample_group.id, actor_id=999, actor_name="admin", db=db_session,
    )

    assert sample_group.title in report.removed_from
    assert second_group.title in report.removed_from
    assert mock_bot.ban_chat_member.call_count == 2

    user_data = {"user_id": user_id, "first_name": "GR", "last_name": "User", "username": "grtest"}
    result_origin = await engine.evaluate(user_data, db_session, group_id=sample_group.id)
    result_other = await engine.evaluate(user_data, db_session, group_id=second_group.id)

    assert result_origin.decision == DecisionEnum.banned
    assert result_other.decision != DecisionEnum.banned


# ─── 5. Unban lifts the row and calls unban_chat_member ───────────────────────

@pytest.mark.asyncio
async def test_unban_lifts_row_and_calls_unban_chat_member(sample_group, db_session, mock_bot):
    user_id = 66004
    await ban_user(
        mock_bot, user_id, "local",
        group_id=sample_group.id, actor_id=999, actor_name="admin", db=db_session,
    )

    report = await unban_user(mock_bot, user_id, actor_id=999, actor_name="admin", db=db_session)

    assert sample_group.title in report.banned_in
    mock_bot.unban_chat_member.assert_called_once()

    ban_row = (await db_session.execute(
        select(UserBan).where(UserBan.telegram_id == user_id)
    )).scalars().first()
    assert ban_row.lifted_at is not None


# ─── 6. Authorization: a non-owner, non-super-admin pressing rev:ban ──────────

@pytest.mark.asyncio
async def test_rev_ban_unauthorized_user_blocked(
    test_session_factory, sample_group, db_session, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)  # distinct from the owner and the attacker

    target = make_tg_user(user_id=77001, first_name="Target")
    db_session.add(TelegramUser(telegram_id=target.id, username=target.username, first_name=target.first_name))
    jr = JoinRequest(
        group_id=sample_group.id, user_id=target.id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.manual_review, score=40.0, matched_rules=[],
    )
    db_session.add(jr)
    await db_session.commit()
    await db_session.refresh(jr)

    attacker = make_tg_user(user_id=99999, first_name="Random", username="randomguy")
    callback = make_callback_query(
        f"rev:ban:{target.id}:{jr.id}:{sample_group.id}",
        user=attacker,
    )

    await cb_manual_review(callback, AsyncMock())

    callback.answer.assert_called_once()
    _args, kwargs = callback.answer.call_args
    answer_text = callback.answer.call_args.args[0] if callback.answer.call_args.args else kwargs.get("text", "")
    assert "הרשא" in answer_text
    callback.message.edit_text.assert_not_called()
    callback.message.edit_reply_markup.assert_not_called()

    async with test_session_factory() as check_db:
        refreshed_jr = await check_db.get(JoinRequest, jr.id)
        assert refreshed_jr.decision == DecisionEnum.manual_review
        assert refreshed_jr.resolved_at is None

        ban_rows = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == target.id)
        )).scalars().all()
        assert ban_rows == []


# ─── 7. CLI: argument parsing, and --dry-run performs zero Telegram calls ─────

def test_cli_argument_parsing():
    import scripts.ban_user as cli

    args = cli.build_parser().parse_args(["@someuser", "--scope", "global", "--dry-run"])
    assert args.identifier == "@someuser"
    assert args.scope == "global"
    assert args.dry_run is True
    assert args.unban is False

    args2 = cli.build_parser().parse_args(["123456", "--unban", "--actor", "42"])
    assert args2.identifier == "123456"
    assert args2.unban is True
    assert args2.actor == 42


@pytest.mark.asyncio
async def test_cli_dry_run_makes_zero_telegram_calls(
    test_session_factory, sample_group, db_session, monkeypatch
):
    import scripts.ban_user as cli

    monkeypatch.setattr(cli, "AsyncSessionLocal", test_session_factory)

    mock_bot_instance = AsyncMock()
    mock_bot_instance.session.close = AsyncMock()
    monkeypatch.setattr(cli, "Bot", lambda token: mock_bot_instance)

    args = cli.build_parser().parse_args([
        "88001", "--scope", "local", "--group", str(sample_group.chat_id),
        "--reason", "test", "--actor", "999", "--dry-run",
    ])

    exit_code = await cli._run(args)

    assert exit_code == 0
    mock_bot_instance.ban_chat_member.assert_not_called()
    mock_bot_instance.decline_chat_join_request.assert_not_called()
    mock_bot_instance.unban_chat_member.assert_not_called()

    async with test_session_factory() as check_db:
        ban_row = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == 88001)
        )).scalars().first()
        assert ban_row is not None
        assert ban_row.scope == "local"


@pytest.mark.asyncio
async def test_cli_initializes_tables_on_fresh_database(tmp_path, monkeypatch):
    """The CLI must create tables itself; it cannot assume the bot already ran init_db()."""
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
    import scripts.ban_user as cli
    import database.init_db as init_db_module

    db_file = tmp_path / "fresh_no_tables.db"
    fresh_engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_file}",
        connect_args={"check_same_thread": False},
    )
    fresh_session_factory = async_sessionmaker(
        fresh_engine, class_=AsyncSession, expire_on_commit=False,
    )

    monkeypatch.setattr(init_db_module, "engine", fresh_engine)
    monkeypatch.setattr(init_db_module, "AsyncSessionLocal", fresh_session_factory)
    monkeypatch.setattr(cli, "AsyncSessionLocal", fresh_session_factory)

    mock_bot_instance = AsyncMock()
    mock_bot_instance.session.close = AsyncMock()
    monkeypatch.setattr(cli, "Bot", lambda token: mock_bot_instance)

    args = cli.build_parser().parse_args([
        "7298365740", "--scope", "global",
        "--reason", "smoke test", "--actor", "845330686", "--dry-run",
    ])

    exit_code = await cli._run(args)

    assert exit_code == 0
    mock_bot_instance.ban_chat_member.assert_not_called()

    async with fresh_session_factory() as check_db:
        ban_row = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == 7298365740)
        )).scalars().first()
        assert ban_row is not None

    await fresh_engine.dispose()


# ─── 8. Service: scope local never touches a second group's rows ─────────────

@pytest.mark.asyncio
async def test_service_local_scope_does_not_leak_into_other_groups(
    test_session_factory, sample_group, second_group, db_session, mock_bot
):
    user_id = 66005
    db_session.add(TelegramUser(telegram_id=user_id, username="localonly", first_name="Local"))
    jr_origin = JoinRequest(
        group_id=sample_group.id, user_id=user_id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.pending, score=10.0, matched_rules=[],
    )
    jr_other = JoinRequest(
        group_id=second_group.id, user_id=user_id, chat_id=second_group.chat_id,
        decision=DecisionEnum.pending, score=10.0, matched_rules=[],
    )
    db_session.add(jr_origin)
    db_session.add(jr_other)
    session_origin = VerificationSession(user_id=user_id, chat_id=sample_group.chat_id, completed_at=None)
    session_other = VerificationSession(user_id=user_id, chat_id=second_group.chat_id, completed_at=None)
    db_session.add(session_origin)
    db_session.add(session_other)
    await db_session.commit()
    for row in (jr_origin, jr_other, session_origin, session_other):
        await db_session.refresh(row)

    report = await ban_user(
        mock_bot, user_id, "local",
        group_id=sample_group.id, reason="spam", actor_id=999, actor_name="admin", db=db_session,
    )

    assert sample_group.title in report.banned_in
    assert second_group.title not in report.banned_in
    assert mock_bot.ban_chat_member.call_count == 1

    async with test_session_factory() as check_db:
        refreshed_origin = await check_db.get(JoinRequest, jr_origin.id)
        assert refreshed_origin.decision == DecisionEnum.banned
        assert refreshed_origin.resolved_at is not None

        refreshed_other = await check_db.get(JoinRequest, jr_other.id)
        assert refreshed_other.decision == DecisionEnum.pending
        assert refreshed_other.resolved_at is None

        refreshed_session_origin = await check_db.get(VerificationSession, session_origin.id)
        assert refreshed_session_origin.completed_at is not None
        assert refreshed_session_origin.passed is False

        refreshed_session_other = await check_db.get(VerificationSession, session_other.id)
        assert refreshed_session_other.completed_at is None
        assert refreshed_session_other.passed is None


# ─── 9. A stale/malformed ban scope callback answers and bans nothing ────────

@pytest.mark.asyncio
async def test_ban_scope_callback_with_unknown_scope_answers_and_bans_nothing(
    test_session_factory, monkeypatch, mock_bot
):
    from config import settings
    admin = make_tg_user(user_id=987654321, first_name="Admin", username="adminuser")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    callback = make_callback_query("ban:scope:55555:unknown_scope", user=admin)

    await cb_ban_scope(callback, mock_bot)

    callback.answer.assert_called_once()
    mock_bot.ban_chat_member.assert_not_called()
    callback.message.edit_text.assert_not_called()

    async with test_session_factory() as check_db:
        ban_rows = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == 55555)
        )).scalars().all()
        assert ban_rows == []


# ─── 10. A group owner cannot ban_scope into a different group via crafted callback data

@pytest.mark.asyncio
async def test_rev_ban_scope_refuses_group_mismatch_for_non_super_admin(
    test_session_factory, sample_group, second_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    monkeypatch.setattr(settings, "super_admin_id", 1)  # distinct from the group owner

    owner = make_tg_user(user_id=sample_group.owner_id, first_name="Owner", username="owner")
    target = make_tg_user(user_id=77002, first_name="Target")
    db_session.add(TelegramUser(telegram_id=target.id, username=target.username, first_name=target.first_name))
    jr = JoinRequest(
        group_id=sample_group.id, user_id=target.id, chat_id=sample_group.chat_id,
        decision=DecisionEnum.manual_review, score=40.0, matched_rules=[],
    )
    db_session.add(jr)
    await db_session.commit()
    await db_session.refresh(jr)

    # The owner of sample_group crafts callback data pointing at second_group instead.
    callback = make_callback_query(
        f"rev:ban_scope:{target.id}:{jr.id}:{second_group.id}:local",
        user=owner,
    )

    await cb_manual_review(callback, mock_bot)

    callback.answer.assert_called_once()
    _args, kwargs = callback.answer.call_args
    answer_text = callback.answer.call_args.args[0] if callback.answer.call_args.args else kwargs.get("text", "")
    assert "הרשא" in answer_text
    mock_bot.ban_chat_member.assert_not_called()
    mock_bot.decline_chat_join_request.assert_not_called()

    async with test_session_factory() as check_db:
        ban_rows = (await check_db.execute(
            select(UserBan).where(UserBan.telegram_id == target.id)
        )).scalars().all()
        assert ban_rows == []

        refreshed_jr = await check_db.get(JoinRequest, jr.id)
        assert refreshed_jr.decision == DecisionEnum.manual_review
        assert refreshed_jr.reviewed_by is None


# ─── 11. Lifting one ban row by id must not lift a wider ban row for the same user

@pytest.mark.asyncio
async def test_unban_by_id_lifts_only_that_row_leaves_wider_ban_active(
    sample_group, second_group, db_session, mock_bot
):
    user_id = 66006
    engine = ModerationEngine()

    await ban_user(
        mock_bot, user_id, "global",
        actor_id=999, actor_name="admin", db=db_session,
    )
    await ban_user(
        mock_bot, user_id, "local",
        group_id=sample_group.id, actor_id=999, actor_name="admin", db=db_session,
    )

    rows = (await db_session.execute(
        select(UserBan).where(UserBan.telegram_id == user_id)
    )).scalars().all()
    local_row = next(r for r in rows if r.scope == "local")
    global_row = next(r for r in rows if r.scope == "global")

    await unban_user(
        mock_bot, user_id, ban_id=local_row.id, actor_id=999, actor_name="admin", db=db_session,
    )

    await db_session.refresh(local_row)
    await db_session.refresh(global_row)
    assert local_row.lifted_at is not None
    assert global_row.lifted_at is None

    user_data = {"user_id": user_id, "first_name": "U", "last_name": "User", "username": "u"}
    result = await engine.evaluate(user_data, db_session, group_id=second_group.id)
    assert result.decision == DecisionEnum.banned


# ─── 12. Lifting a global_remove row by id unbans in every group, not only the origin

@pytest.mark.asyncio
async def test_ban_lift_global_remove_row_unbans_every_group(
    test_session_factory, sample_group, second_group, db_session, mock_bot, monkeypatch
):
    from config import settings
    admin = make_tg_user(user_id=987654321, first_name="Admin", username="adminuser")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    user_id = 66007
    await ban_user(
        mock_bot, user_id, "global_remove",
        group_id=sample_group.id, actor_id=admin.id, actor_name="admin", db=db_session,
    )

    ban_row = (await db_session.execute(
        select(UserBan).where(UserBan.telegram_id == user_id)
    )).scalars().first()
    assert ban_row.scope == "global_remove"

    callback = make_callback_query(f"ban:lift:{ban_row.id}", user=admin)

    await cb_ban_lift(callback, mock_bot)

    assert mock_bot.unban_chat_member.call_count == 2
    unbanned_chat_ids = {call.args[0] for call in mock_bot.unban_chat_member.call_args_list}
    assert unbanned_chat_ids == {sample_group.chat_id, second_group.chat_id}

    async with test_session_factory() as check_db:
        refreshed = await check_db.get(UserBan, ban_row.id)
        assert refreshed.lifted_at is not None


# ─── 13. Lifting a global_remove row without ban_id unbans in every group, not only the origin

@pytest.mark.asyncio
async def test_unban_global_remove_without_ban_id_unbans_every_group(
    sample_group, second_group, db_session, mock_bot
):
    user_id = 66008
    await ban_user(
        mock_bot, user_id, "global_remove",
        group_id=sample_group.id, actor_id=999, actor_name="admin", db=db_session,
    )

    ban_row = (await db_session.execute(
        select(UserBan).where(UserBan.telegram_id == user_id)
    )).scalars().first()
    assert ban_row.scope == "global_remove"

    await unban_user(mock_bot, user_id, actor_id=999, actor_name="admin", db=db_session)

    await db_session.refresh(ban_row)
    assert ban_row.lifted_at is not None

    assert mock_bot.unban_chat_member.call_count == 2
    unbanned_chat_ids = {call.args[0] for call in mock_bot.unban_chat_member.call_args_list}
    assert unbanned_chat_ids == {sample_group.chat_id, second_group.chat_id}


# ─── 14. "User not found" replies escape HTML-unsafe identifiers ──────────────
#
# Production log: an admin ran "/ban <@yael43>"; the "not found" reply
# interpolated the raw identifier into a message sent with the bot's default
# HTML parse_mode, so Telegram's "<@yael43" was parsed as an unsupported
# start tag and the whole update crashed with an unhandled TelegramBadRequest
# (the admin never got a reply). Same root cause as the hardcoded
# "<telegram_id|@username>" usage strings, which would crash the same way on
# a plain "/ban" with no arguments.

@pytest.mark.asyncio
async def test_ban_not_found_reply_escapes_html_unsafe_identifier(
    test_session_factory, mock_bot,
):
    message = AsyncMock(spec=Message)
    message.text = "/ban <@yael43>"
    message.from_user = make_tg_user(user_id=424242, username="admin")
    message.chat = make_tg_chat()
    message.reply = AsyncMock()

    await cmd_ban(message, mock_bot)

    message.reply.assert_called_once()
    sent_text = message.reply.call_args.args[0]
    assert "<@yael43>" not in sent_text
    assert "&lt;@yael43&gt;" in sent_text


@pytest.mark.asyncio
async def test_ban_usage_message_escapes_literal_angle_brackets(
    test_session_factory, mock_bot,
):
    message = AsyncMock(spec=Message)
    message.text = "/ban"
    message.from_user = make_tg_user(user_id=424242, username="admin")
    message.chat = make_tg_chat()
    message.reply = AsyncMock()

    await cmd_ban(message, mock_bot)

    sent_text = message.reply.call_args.args[0]
    assert "<telegram_id" not in sent_text
    assert "&lt;telegram_id" in sent_text


@pytest.mark.asyncio
async def test_unban_not_found_reply_escapes_html_unsafe_identifier(
    test_session_factory, mock_bot, monkeypatch,
):
    admin = make_tg_user(user_id=424242, username="admin")
    monkeypatch.setattr(settings, "super_admin_id", admin.id)

    message = AsyncMock(spec=Message)
    message.text = "/unban <@yael43>"
    message.from_user = admin
    message.chat = make_tg_chat()
    message.reply = AsyncMock()

    await cmd_unban(message, mock_bot)

    message.reply.assert_called_once()
    sent_text = message.reply.call_args.args[0]
    assert "<@yael43>" not in sent_text
    assert "&lt;@yael43&gt;" in sent_text
