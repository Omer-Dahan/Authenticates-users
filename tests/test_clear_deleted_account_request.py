"""Direct unit tests for the shared Telegram-clearing helpers in
moderation/actions.py (clear_deleted_account_request, decline_expired_request,
probe_is_deleted_account). These pin the contract every caller (purge,
reconciliation, verification/cleanup) relies on, independent of the sweep
machinery around them."""
import pytest
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError, TelegramRetryAfter
from aiogram.types import ChatMemberLeft, ChatMemberMember, User

from moderation.actions import (
    PURGE_MAX_RETRY_AFTER, clear_deleted_account_request, decline_expired_request,
    probe_is_deleted_account,
)


def _deleted_member() -> ChatMemberMember:
    user = User(id=0, is_bot=False, first_name="", username=None, last_name=None)
    return ChatMemberMember(status="member", user=user)


def _left_named(user_id: int) -> ChatMemberLeft:
    return ChatMemberLeft(status="left", user=User(id=user_id, is_bot=False, first_name="Dana"))


# ─── clear_deleted_account_request: caller already proved the account deleted ─

@pytest.mark.asyncio
async def test_clear_decline_success(mock_bot):
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is True
    assert outcome.method == "decline"
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_clear_decline_already_gone(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: HIDE_REQUESTER_MISSING"
    )
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is True
    assert outcome.method == "already_gone"
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_clear_decline_access_error_never_escalates(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: chat not found"
    )
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is False
    assert outcome.access_error is True
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_clear_decline_deactivated_escalates_to_ban_and_succeeds(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is True
    assert outcome.method == "ban"
    mock_bot.ban_chat_member.assert_called_once_with(-100, 1)


@pytest.mark.asyncio
async def test_clear_decline_generic_error_never_escalates_to_ban(mock_bot):
    """Only an explicit "user is deactivated" refusal justifies the ban
    fallback. A rights error under a different string, a network error, an
    exhausted 429, or USER_ID_INVALID must never be treated as proof of
    deactivation and must never trigger a ban - the row is reported as a
    plain failure instead."""
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: CHAT_ADMIN_REQUIRED"
    )
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is False
    assert outcome.access_error is False
    assert outcome.ban_error is None
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_clear_decline_exhausted_retry_never_escalates_to_ban(mock_bot):
    """A concrete bad case from the review: a decline that keeps hitting real
    flood control (TelegramRetryAfter, not a generic error) and exhausts every
    retry in _call_honouring_flood must still not escalate to a ban - a flood
    error says nothing about whether the account is deactivated."""
    mock_bot.decline_chat_join_request.side_effect = TelegramRetryAfter(
        method="declineChatJoinRequest", message="Too Many Requests", retry_after=0,
    )
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is False
    assert mock_bot.decline_chat_join_request.call_count == PURGE_MAX_RETRY_AFTER
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_clear_decline_network_error_never_escalates_to_ban(mock_bot):
    """A network-level failure (TelegramNetworkError) must never be treated
    as proof of deactivation either."""
    mock_bot.decline_chat_join_request.side_effect = TelegramNetworkError(
        method="declineChatJoinRequest", message="Connection reset by peer"
    )
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is False
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_clear_decline_deactivated_and_ban_also_refused(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )
    mock_bot.ban_chat_member.side_effect = TelegramAPIError(
        method="banChatMember", message="Forbidden: user is deactivated"
    )
    outcome = await clear_deleted_account_request(mock_bot, -100, 1)
    assert outcome.cleared is False
    assert outcome.ban_error is not None


# ─── probe_is_deleted_account ──────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_probe_true_for_empty_profile(mock_bot):
    mock_bot.get_chat_member.return_value = _deleted_member()
    assert await probe_is_deleted_account(mock_bot, -100, 1) is True


@pytest.mark.asyncio
async def test_probe_false_for_live_account(mock_bot):
    mock_bot.get_chat_member.return_value = _left_named(1)
    assert await probe_is_deleted_account(mock_bot, -100, 1) is False


@pytest.mark.asyncio
async def test_probe_none_on_access_error(mock_bot):
    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Bad Request: chat not found"
    )
    assert await probe_is_deleted_account(mock_bot, -100, 1) is None


# ─── decline_expired_request: caller has NOT proved deletion yet ──────────────

@pytest.mark.asyncio
async def test_decline_expired_success_never_probes(mock_bot):
    outcome = await decline_expired_request(mock_bot, -100, 1)
    assert outcome.cleared is True
    assert outcome.method == "decline"
    mock_bot.get_chat_member.assert_not_called()
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_decline_expired_failure_on_live_account_never_bans(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: some unexpected error"
    )
    mock_bot.get_chat_member.return_value = _left_named(1)

    outcome = await decline_expired_request(mock_bot, -100, 1)

    assert outcome.cleared is False
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_decline_expired_failure_on_inconclusive_probe_never_bans(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: some unexpected error"
    )
    mock_bot.get_chat_member.side_effect = TelegramAPIError(
        method="getChatMember", message="Bad Request: network error"
    )
    mock_bot.get_chat.side_effect = TelegramAPIError(
        method="getChat", message="Bad Request: network error"
    )

    outcome = await decline_expired_request(mock_bot, -100, 1)

    assert outcome.cleared is False
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_decline_expired_failure_on_deleted_account_bans(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Forbidden: user is deactivated"
    )
    mock_bot.get_chat_member.return_value = _deleted_member()

    outcome = await decline_expired_request(mock_bot, -100, 1)

    assert outcome.cleared is True
    assert outcome.method == "ban"
    mock_bot.ban_chat_member.assert_called_once_with(-100, 1)


@pytest.mark.asyncio
async def test_decline_expired_network_error_never_probes_or_bans(mock_bot):
    """Review finding: decline_expired_request used to probe
    probe_is_deleted_account on ANY non-access decline failure and ban if the
    profile looked empty, regardless of why decline failed. A network error
    must use the exact same gate as clear_deleted_account_request - the
    explicit deactivation refusal only - and never even reach the probe."""
    mock_bot.decline_chat_join_request.side_effect = TelegramNetworkError(
        method="declineChatJoinRequest", message="Connection reset by peer"
    )

    outcome = await decline_expired_request(mock_bot, -100, 1)

    assert outcome.cleared is False
    mock_bot.get_chat_member.assert_not_called()
    mock_bot.ban_chat_member.assert_not_called()


@pytest.mark.asyncio
async def test_decline_expired_access_error_never_probes_or_bans(mock_bot):
    mock_bot.decline_chat_join_request.side_effect = TelegramAPIError(
        method="declineChatJoinRequest", message="Bad Request: chat not found"
    )

    outcome = await decline_expired_request(mock_bot, -100, 1)

    assert outcome.cleared is False
    assert outcome.access_error is True
    mock_bot.get_chat_member.assert_not_called()
    mock_bot.ban_chat_member.assert_not_called()
