"""Shared ban/unban service used by every manual-ban access path: commands, the
manual-review button, the super-admin panel, and the terminal script.
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy import select, or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import (
    DecisionEnum, Group, JoinRequest, ModerationLog, TelegramUser, UserBan,
    VerificationSession,
)
from logs import get_logger

logger = get_logger(__name__)

VALID_SCOPES = ("global", "global_remove", "local")


@dataclass
class BanReport:
    banned_in: list = field(default_factory=list)
    removed_from: list = field(default_factory=list)
    failed: list = field(default_factory=list)
    skipped: list = field(default_factory=list)


async def _find_active_ban_row(
    db: AsyncSession, telegram_id: int, scope: str, effective_group_id: Optional[int],
    *, bot_issued_only: bool = False,
) -> Optional[UserBan]:
    """Finds the active ban row this (telegram_id, scope, group) identifies.

    "local" and "global_remove" share the same per-group identity on
    purpose: both are keyed on (telegram_id, group_id), so escalating one to
    the other for the same group finds and updates the existing row instead
    of leaving a stale duplicate behind (see ban_user's upsert below).

    bot_issued_only=True narrows the match to a row this codebase's own
    deleted-account-clearing path created (banned_by IS NULL - see
    record_deleted_account_ban) so that path can never find, and overwrite,
    an admin's own manual ban for the same user and group even when it has
    the exact same scope and group_id shape.
    """
    if scope == "global":
        query = select(UserBan).where(
            UserBan.telegram_id == telegram_id,
            UserBan.lifted_at.is_(None),
            UserBan.scope == "global",
        )
    else:
        query = select(UserBan).where(
            UserBan.telegram_id == telegram_id,
            UserBan.lifted_at.is_(None),
            UserBan.scope.in_(("local", "global_remove")),
            UserBan.group_id == effective_group_id,
        )
    if bot_issued_only:
        query = query.where(UserBan.banned_by.is_(None))
    result = await db.execute(query)
    return result.scalars().first()


async def _close_open_join_requests(
    db: AsyncSession, telegram_id: int, scope: str, origin_group_id: Optional[int],
    actor_name: Optional[str], now: datetime,
) -> None:
    query = select(JoinRequest).where(
        JoinRequest.user_id == telegram_id,
        JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
    )
    if scope == "local":
        query = query.where(JoinRequest.group_id == origin_group_id)
    result = await db.execute(query)
    for jr in result.scalars().all():
        jr.decision = DecisionEnum.banned
        jr.resolved_at = now
        db.add(ModerationLog(
            group_id=jr.group_id,
            join_request_id=jr.id,
            user_id=telegram_id,
            decision=DecisionEnum.banned,
            score=jr.score,
            matched_rules=jr.matched_rules,
            details={"manual_ban": True, "scope": scope, "actor": actor_name},
        ))


async def _close_open_verification_sessions(
    db: AsyncSession, telegram_id: int, scope: str, origin_chat_id: Optional[int], now: datetime,
) -> None:
    query = select(VerificationSession).where(
        VerificationSession.user_id == telegram_id,
        VerificationSession.completed_at.is_(None),
    )
    if scope == "local":
        query = query.where(VerificationSession.chat_id == origin_chat_id)
    result = await db.execute(query)
    for session in result.scalars().all():
        session.completed_at = now
        session.passed = False


async def ban_user(
    bot: Bot,
    telegram_id: int,
    scope: str,
    *,
    group_id: Optional[int] = None,
    reason: Optional[str] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    db: AsyncSession = None,
    dry_run: bool = False,
) -> BanReport:
    if scope not in VALID_SCOPES:
        raise ValueError(f"Unknown ban scope: {scope}")
    if scope in ("local", "global_remove") and group_id is None:
        raise ValueError(f"scope={scope} requires an origin group_id")

    report = BanReport()
    now = datetime.now(timezone.utc)
    effective_group_id = None if scope == "global" else group_id

    user_result = await db.execute(
        select(TelegramUser).where(TelegramUser.telegram_id == telegram_id)
    )
    known_user = user_result.scalar_one_or_none()

    origin_group = await db.get(Group, group_id) if scope != "global" else None

    existing = await _find_active_ban_row(db, telegram_id, scope, effective_group_id)
    if existing:
        existing.scope = scope
        existing.group_id = effective_group_id
        existing.reason = reason
        existing.banned_by = actor_id
        existing.banned_by_name = actor_name
        if known_user:
            existing.username = known_user.username
            existing.first_name = known_user.first_name
    else:
        db.add(UserBan(
            telegram_id=telegram_id,
            username=known_user.username if known_user else None,
            first_name=known_user.first_name if known_user else None,
            scope=scope,
            group_id=effective_group_id,
            reason=reason,
            banned_by=actor_id,
            banned_by_name=actor_name,
        ))

    await _close_open_join_requests(db, telegram_id, scope, group_id, actor_name, now)
    await _close_open_verification_sessions(
        db, telegram_id, scope, origin_group.chat_id if origin_group else None, now,
    )
    await db.flush()

    if dry_run:
        await db.commit()
        return report

    if scope in ("global", "global_remove"):
        all_groups = (await db.execute(select(Group))).scalars().all()
        target_groups = []
        for g in all_groups:
            if g.is_active and not g.is_banned:
                target_groups.append(g)
            else:
                report.skipped.append(g.title or str(g.chat_id))
    else:
        target_groups = [origin_group] if origin_group else []

    for g in target_groups:
        title = g.title or str(g.chat_id)
        try:
            await bot.ban_chat_member(g.chat_id, telegram_id)
        except TelegramAPIError as e:
            logger.warning("Failed to ban user in group", chat_id=g.chat_id, error=str(e))
            report.failed.append((title, str(e)))
            continue

        report.banned_in.append(title)
        try:
            await bot.decline_chat_join_request(g.chat_id, telegram_id)
        except TelegramAPIError:
            pass  # a missing pending request is not an error
        report.removed_from.append(title)

    await db.commit()
    return report


async def unban_user(
    bot: Bot,
    telegram_id: int,
    *,
    group_id: Optional[int] = None,
    ban_id: Optional[int] = None,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    db: AsyncSession = None,
    dry_run: bool = False,
) -> BanReport:
    report = BanReport()
    now = datetime.now(timezone.utc)

    if ban_id is not None:
        ban_row = await db.get(UserBan, ban_id)
        active_bans = [ban_row] if ban_row and ban_row.lifted_at is None else []
    else:
        query = select(UserBan).where(
            UserBan.telegram_id == telegram_id,
            UserBan.lifted_at.is_(None),
        )
        if group_id is not None:
            query = query.where(or_(UserBan.scope == "global", UserBan.group_id == group_id))
        active_bans = (await db.execute(query)).scalars().all()

    if not active_bans:
        await db.commit()
        return report

    groups_to_unban = {}
    for ban_row in active_bans:
        ban_row.lifted_at = now
        ban_row.lifted_by = actor_id
        # The group set to unban depends only on the row's own scope, never on
        # how the row was selected: "global" and "global_remove" both mean
        # every managed group, "local" means the row's own group.
        if ban_row.scope in ("global", "global_remove"):
            all_groups = (await db.execute(select(Group))).scalars().all()
            for g in all_groups:
                groups_to_unban[g.id] = g
        elif ban_row.group_id is not None:
            g = await db.get(Group, ban_row.group_id)
            if g:
                groups_to_unban[g.id] = g

    await db.flush()

    if dry_run:
        await db.commit()
        return report

    for g in groups_to_unban.values():
        title = g.title or str(g.chat_id)
        try:
            await bot.unban_chat_member(g.chat_id, telegram_id, only_if_banned=True)
            report.banned_in.append(title)
        except TelegramAPIError as e:
            logger.warning("Failed to unban user in group", chat_id=g.chat_id, error=str(e))
            report.failed.append((title, str(e)))

    await db.commit()
    return report


# ─── Purge pending requests from deleted accounts ──────────────────────────────

# What the Bot API can and cannot tell us (Bot API 7.10):
# - There is no method that lists pending join requests or chat members. The
#   bot only learns about a request from the chat_join_request update, which is
#   sent when the request is CREATED. Nothing is sent when another admin
#   approves/declines it inside the Telegram app or when the user withdraws it,
#   so an open JoinRequest row is "a request the bot once saw", not "a request
#   Telegram still has".
# - getChatMember on a pending requester returns status "left" (not a member).
#   It proves the account exists, never that the request is still pending.
# - A probe that RESOLVES with an empty profile (no first_name, username, or
#   last_name) is the only evidence of a deleted account: first_name is a
#   required field for every live account. This is a heuristic, not a
#   documented API guarantee - see docs/DELETED-REQUEST-REMOVAL.md for what was
#   actually measured and what was only inferred.
# - A probe that resolves with a member status proves the request is gone: a
#   member cannot have a pending request for the same chat.
# - declineChatJoinRequest is the decisive call for "does the request still
#   exist": True means it existed and is now declined, HIDE_REQUESTER_MISSING
#   means Telegram no longer has it. It is only ever sent for deleted accounts,
#   so a live applicant is never declined by this purge.
# - Measured live against the real Telegram API, on accounts already kicked
#   (not on a request still pending - the full "pending -> decline refused ->
#   ban -> request gone" lifecycle was never observed end to end, see
#   docs/DELETED-REQUEST-REMOVAL.md): declineChatJoinRequest can fail with
#   "Forbidden: user is deactivated", and banChatMember still succeeds against
#   the same account. clear_deleted_account_request() below treats that one
#   exact refusal, and only that one, as grounds to escalate to banChatMember.
#   The reasoning (an assumption, not something measured end to end): a banned
#   user cannot simultaneously hold a pending join request, so a successful
#   ban is the best evidence available that the request is gone. A row is
#   only ever closed once the escalation proves the request is actually gone -
#   a decline refusal alone proves nothing and must never close the row on
#   its own.
# These two error substrings mean an access problem (group access lost, or the
# bot lacks admin rights), never "the user is gone": never infer deletion from
# them, and never even attempt the fallback probe or the ban escalation.
_ACCESS_ERROR_MARKERS = ("not enough rights", "chat not found")
# These three mean Telegram has no record of this user in this specific chat
# at all (confirmed live on real sampled rows - see
# docs/DELETED-REQUEST-REMOVAL.md): not an access problem, not proof of
# deletion, and not a transient probe error either. The bot cannot act on a
# request it cannot even resolve; only the Telegram app can. Must never
# poison a group's stale-rule eligibility the way a genuine probe error does.
_UNRESOLVABLE_MARKERS = ("participant_id_invalid", "user_id_invalid", "member not found")
# Only this exact refusal justifies escalating a refused decline to a ban -
# see clear_deleted_account_request() below. Every other decline failure (a
# rights error under a different string, a network error, an exhausted 429,
# USER_ID_INVALID, ...) must leave the row open, never ban.
_DEACTIVATED_MARKER = "user is deactivated"
_REQUEST_MISSING_MARKER = "hide_requester_missing"
_MEMBER_STATUSES = ("member", "administrator", "creator")

PURGE_NOTE = "account deleted (empty profile)"
PURGE_STALE_NOTE = "account deleted, request no longer on Telegram"
PURGE_BAN_NOTE = "account deleted, decline was refused by Telegram (deactivated account); cleared by banning instead"
ALREADY_MEMBER_NOTE = "already a member, request resolved outside the bot"


@dataclass
class PurgeReport:
    checked: int = 0                                     # open rows the bot holds
    deleted_found: list = field(default_factory=list)   # (title, user_id, name_from_db)
    removed: list = field(default_factory=list)          # (title, user_id) declined, Telegram still had it
    request_gone: list = field(default_factory=list)     # (title, user_id) deleted, Telegram no longer had it
    removed_via_ban: list = field(default_factory=list)  # (title, user_id) decline refused, cleared by a ban instead
    already_member: list = field(default_factory=list)   # (title, user_id) request resolved outside the bot
    decline_failed: list = field(default_factory=list)   # (title, user_id, error) decline AND ban both failed, row left open
    unresolvable: list = field(default_factory=list)     # (title, user_id) Telegram has no record of this user in this chat; the bot cannot act, row left open
    race_lost_already_member: list = field(default_factory=list)  # (title, user_id) an already_member row lost the race; NOT part of deleted_found
    race_lost_deleted: list = field(default_factory=list)         # (title, user_id) a deleted-account row lost the race; part of deleted_found's own breakdown
    alive: int = 0                                       # account exists, request state unknown
    inconclusive: int = 0                                # Telegram returned nothing usable
    access_errors: list = field(default_factory=list)    # (title, error_string)
    per_group: dict = field(default_factory=dict)        # keyed by Group.id, carries "title"
    stopped_early: bool = False


def _looks_empty(first_name: Optional[str], username: Optional[str], last_name: Optional[str]) -> bool:
    return not first_name and not username and not last_name


def _is_member(member) -> bool:
    if member.status in _MEMBER_STATUSES:
        return True
    return member.status == "restricted" and bool(getattr(member, "is_member", False))


def is_request_missing_error(err: str) -> bool:
    return _REQUEST_MISSING_MARKER in err.lower()


async def _close_row(db: AsyncSession, jr: JoinRequest, decision: DecisionEnum, note: str,
                     details: dict, now: datetime) -> bool:
    """Closes jr only if it is still open (resolved_at IS NULL) at the moment
    of the write. A conditional UPDATE, not a plain attribute assignment, so a
    decision made elsewhere (a manual approve/reject/ban) while this sweep's
    probes were in flight always wins, and the same row is never closed
    twice. Returns whether this call actually closed it; the caller must
    skip its own follow-up bookkeeping (sessions, counters, logs) when it
    did not."""
    result = await db.execute(
        update(JoinRequest)
        .where(JoinRequest.id == jr.id, JoinRequest.resolved_at.is_(None))
        .values(decision=decision, resolved_at=now, notes=note)
    )
    if result.rowcount == 0:
        return False
    db.add(ModerationLog(
        group_id=jr.group_id,
        join_request_id=jr.id,
        user_id=jr.user_id,
        decision=decision,
        score=jr.score,
        matched_rules=jr.matched_rules,
        details=details,
    ))
    return True


async def _close_sessions(db: AsyncSession, user_id: int, chat_id: int, now: datetime, passed) -> None:
    session_result = await db.execute(select(VerificationSession).where(
        VerificationSession.user_id == user_id,
        VerificationSession.chat_id == chat_id,
        VerificationSession.completed_at.is_(None),
    ))
    for session in session_result.scalars().all():
        session.completed_at = now
        session.passed = passed


PURGE_CONCURRENCY = 5         # Telegram calls in flight at once during a purge sweep
PURGE_MAX_RETRY_AFTER = 5     # 429s honoured per call before the call counts as failed

ProgressCallback = Callable[[int, int], Awaitable[None]]


async def _call_honouring_flood(call: Callable[[], Awaitable], attempts: int = PURGE_MAX_RETRY_AFTER):
    """Runs one Telegram call, sleeping out every 429 instead of failing the row.

    After `attempts` consecutive 429s the last one is raised and the caller's
    normal error handling applies (the row stays open)."""
    for attempt in range(attempts):
        try:
            return await call()
        except TelegramRetryAfter as e:
            if attempt == attempts - 1:
                raise
            logger.warning(f"purge_deleted: flood control, sleeping {e.retry_after}s")
            await asyncio.sleep(e.retry_after)


@dataclass
class ClearOutcome:
    """What actually happened when clearing a join request already proven to
    belong to a deleted account. `cleared=True` means Telegram no longer has
    the request - that is the only thing that justifies closing the local
    row; `cleared=False` means the row must stay open."""
    cleared: bool
    method: Optional[str] = None       # "decline" | "already_gone" | "ban" | None
    decline_error: Optional[str] = None
    ban_error: Optional[str] = None
    access_error: bool = False          # decline failed for a rights/access reason - ban was never attempted


async def clear_deleted_account_request(bot: Bot, chat_id: int, user_id: int) -> ClearOutcome:
    """Clears a join request already proven to belong to a deleted account
    (see _looks_empty above). declineChatJoinRequest is the correct, decisive
    call and is always tried first. Measured live against the real Telegram
    API, but only on accounts that were already kicked, not on a request
    still genuinely pending (see docs/DELETED-REQUEST-REMOVAL.md for exactly
    what was and was not observed): decline can fail with "Forbidden: user is
    deactivated", and separately, banChatMember still succeeds against the
    same account.

    This function only escalates decline's failure to banChatMember when the
    error is that exact, explicit refusal (_DEACTIVATED_MARKER) - a call
    that, unlike decline, targets the chat member record rather than the
    request record, and keeps working for users who were never members at
    all (that is the whole point of a pre-emptive ban). The reasoning for
    treating a successful ban as proof the request is gone is an assumption,
    not something measured end to end: a banned user cannot simultaneously
    hold a pending join request, so a successful ban is the best evidence
    this bot can get, short of Telegram exposing the pending-request list
    directly (it does not, for any bot).

    Never escalates to ban when decline failed for a rights/access reason
    (_ACCESS_ERROR_MARKERS): a bot that cannot decline for that reason cannot
    ban either, and must not try - same rule as the probe above it.
    Never escalates when the request is already gone (HIDE_REQUESTER_MISSING
    or equivalent): there is nothing left to clear.
    Never escalates for any OTHER failure either (a rights error under a
    different string, a network error, an exhausted 429, USER_ID_INVALID,
    ...): only the one explicit deactivation refusal is trusted, because the
    "empty profile" heuristic that got the caller here is itself not
    airtight, and a real member with a coincidentally empty profile must
    never be banned on the strength of an unrelated error alone.
    """
    try:
        await _call_honouring_flood(lambda: bot.decline_chat_join_request(chat_id, user_id))
        return ClearOutcome(cleared=True, method="decline")
    except TelegramAPIError as e:
        decline_error = str(e)

    if is_request_missing_error(decline_error):
        return ClearOutcome(cleared=True, method="already_gone", decline_error=decline_error)

    if any(marker in decline_error.lower() for marker in _ACCESS_ERROR_MARKERS):
        return ClearOutcome(cleared=False, decline_error=decline_error, access_error=True)

    if _DEACTIVATED_MARKER not in decline_error.lower():
        return ClearOutcome(cleared=False, decline_error=decline_error)

    try:
        await _call_honouring_flood(lambda: bot.ban_chat_member(chat_id, user_id))
        return ClearOutcome(cleared=True, method="ban", decline_error=decline_error)
    except TelegramAPIError as e:
        return ClearOutcome(cleared=False, decline_error=decline_error, ban_error=str(e))


async def record_deleted_account_ban(
    db: AsyncSession, group_id: int, telegram_id: int, reason: str, actor_name: str,
) -> None:
    """Records a ban issued by the deleted-account clearing path (purge,
    reconciliation, or verification timeout) the same way every other ban
    access path does (see ban_user above) - so /unban can find it and a
    future admin can see what happened, instead of a banChatMember call that
    leaves no trace anywhere but Telegram's own side.

    Scoped to the one group where the request existed (scope="local"), never
    wider: this path never had authorization to ban anywhere else. Idempotent
    like ban_user's own upsert: an existing active row this same path already
    created for this exact (telegram_id, group_id) pair is updated in place,
    never duplicated - bot_issued_only=True makes sure that lookup can only
    ever find a row this path itself created (banned_by IS NULL), never an
    admin's own manual ban for the same user and group, even one with the
    exact same scope="local" + group_id shape.
    """
    existing = await _find_active_ban_row(db, telegram_id, "local", group_id, bot_issued_only=True)
    if existing:
        existing.reason = reason
        existing.banned_by_name = actor_name
        return
    user_result = await db.execute(select(TelegramUser).where(TelegramUser.telegram_id == telegram_id))
    known_user = user_result.scalar_one_or_none()
    db.add(UserBan(
        telegram_id=telegram_id,
        username=known_user.username if known_user else None,
        first_name=known_user.first_name if known_user else None,
        scope="local",
        group_id=group_id,
        reason=reason,
        banned_by=None,
        banned_by_name=actor_name,
    ))


async def probe_is_deleted_account(bot: Bot, chat_id: int, user_id: int) -> Optional[bool]:
    """Read-only answer to exactly one question: does this user's profile
    look like a deleted account (empty first_name/username/last_name - see
    _looks_empty above)? Returns True (deleted), False (a live, resolvable
    account), or None when Telegram gave no usable answer (access problem,
    or both getChatMember and the getChat fallback failed). Callers must
    treat None exactly like False for anything destructive: an inconclusive
    answer is never grounds to ban or decline."""
    try:
        member = await _call_honouring_flood(lambda: bot.get_chat_member(chat_id, user_id))
    except TelegramAPIError as e:
        if any(marker in str(e).lower() for marker in _ACCESS_ERROR_MARKERS):
            return None
        await asyncio.sleep(0.05)
        try:
            chat_info = await _call_honouring_flood(lambda: bot.get_chat(user_id))
        except TelegramAPIError:
            return None
        return _looks_empty(chat_info.first_name, chat_info.username, chat_info.last_name)
    else:
        user = member.user
        return _looks_empty(user.first_name, user.username, user.last_name)


async def decline_expired_request(bot: Bot, chat_id: int, user_id: int) -> ClearOutcome:
    """Used where a join request is being closed for a reason that has
    nothing to do with proving the account deleted (e.g. a verification
    session that simply timed out - see verification/cleanup.py).

    There is exactly one rule in this codebase for when a refused decline may
    be escalated to a ban (see clear_deleted_account_request above for the
    full reasoning): only the explicit deactivation refusal
    (_DEACTIVATED_MARKER) justifies it, never a live applicant's request
    being declined for timing out. This function has no separate logic of
    its own - it calls clear_deleted_account_request directly so that rule
    is defined once, not copied.
    """
    return await clear_deleted_account_request(bot, chat_id, user_id)


@dataclass
class _PurgeOutcome:
    kind: str                          # deleted | already_member | alive | inconclusive | unresolvable
    access_error: Optional[str] = None
    member_status: Optional[str] = None
    clear: Optional[ClearOutcome] = None


async def _probe_row(bot: Bot, chat_id: int, user_id: int, dry_run: bool) -> _PurgeOutcome:
    """Telegram side of one row only. No database access here: the rows are
    probed concurrently and the session is not safe to share between them."""
    try:
        member = await _call_honouring_flood(lambda: bot.get_chat_member(chat_id, user_id))
    except TelegramAPIError as e:
        err = str(e)
        if any(marker in err.lower() for marker in _ACCESS_ERROR_MARKERS):
            return _PurgeOutcome("inconclusive", access_error=err)

        await asyncio.sleep(0.05)
        try:
            chat_info = await _call_honouring_flood(lambda: bot.get_chat(user_id))
        except TelegramAPIError:
            if any(marker in err.lower() for marker in _UNRESOLVABLE_MARKERS):
                # Telegram has no record of this user in this chat at all -
                # not an access problem, not proof of deletion, and not a
                # transient probe error either. The bot cannot act on it.
                return _PurgeOutcome("unresolvable")
            return _PurgeOutcome("inconclusive")

        if not _looks_empty(chat_info.first_name, chat_info.username, chat_info.last_name):
            return _PurgeOutcome("alive")
    else:
        user = member.user
        if _looks_empty(user.first_name, user.username, user.last_name):
            pass  # deleted account: clear_deleted_account_request below is the decisive call
        elif _is_member(member):
            return _PurgeOutcome("already_member", member_status=member.status)
        else:
            return _PurgeOutcome("alive")

    outcome = _PurgeOutcome("deleted")
    if dry_run:
        return outcome

    outcome.clear = await clear_deleted_account_request(bot, chat_id, user_id)
    return outcome


async def purge_deleted_requests(
    bot: Bot,
    *,
    group_id: Optional[int] = None,
    dry_run: bool = True,
    actor_id: Optional[int] = None,
    actor_name: Optional[str] = None,
    db: AsyncSession = None,
    max_probes: int = 2000,
    concurrency: int = PURGE_CONCURRENCY,
    progress: Optional[ProgressCallback] = None,
    progress_interval: float = 3.0,
) -> PurgeReport:
    report = PurgeReport()
    now = datetime.now(timezone.utc)

    group_query = select(Group).where(Group.is_active, ~Group.is_banned)
    if group_id is not None:
        group_query = group_query.where(Group.id == group_id)
    groups = (await db.execute(group_query)).scalars().all()

    # Phase 1 (database): collect every open row, up to the probe budget.
    rows: list[tuple[Group, JoinRequest]] = []
    for g in groups:
        report.per_group.setdefault(g.id, {
            "title": g.title or str(g.chat_id),
            "checked": 0, "deleted_found": 0, "removed": 0, "request_gone": 0,
            "removed_via_ban": 0, "already_member": 0, "decline_failed": 0,
            "unresolvable": 0, "race_lost_already_member": 0, "race_lost_deleted": 0,
            "alive": 0, "inconclusive": 0, "access_error": None,
        })

        jr_result = await db.execute(select(JoinRequest).where(
            JoinRequest.group_id == g.id,
            JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
            JoinRequest.resolved_at.is_(None),
        ))
        for jr in jr_result.scalars().all():
            if len(rows) >= max_probes:
                report.stopped_early = True
                break
            rows.append((g, jr))
        if report.stopped_early:
            break

    # Phase 2 (Telegram): probe in parallel with bounded concurrency.
    total = len(rows)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0
    last_progress = time.monotonic()

    async def probe(g: Group, jr: JoinRequest) -> _PurgeOutcome:
        nonlocal done, last_progress
        async with semaphore:
            try:
                outcome = await _probe_row(bot, g.chat_id, jr.user_id, dry_run)
            except Exception:  # never let one row sink the whole sweep; the row stays open
                logger.exception(f"purge_deleted: probe failed for {jr.user_id} in {g.chat_id}")
                outcome = _PurgeOutcome("inconclusive")
            await asyncio.sleep(0.05)
        done += 1
        if progress is not None and time.monotonic() - last_progress >= progress_interval:
            last_progress = time.monotonic()
            try:
                await progress(done, total)
            except Exception:
                logger.exception("purge_deleted: progress callback failed")
        return outcome

    outcomes = await asyncio.gather(*(probe(g, jr) for g, jr in rows))

    # Phase 3 (database): apply the outcomes sequentially, in row order.
    for (g, jr), outcome in zip(rows, outcomes):
        title = g.title or str(g.chat_id)
        stats = report.per_group[g.id]
        report.checked += 1
        stats["checked"] += 1

        if outcome.kind == "inconclusive":
            if outcome.access_error and stats["access_error"] is None:
                stats["access_error"] = outcome.access_error
                report.access_errors.append((title, outcome.access_error))
            report.inconclusive += 1
            stats["inconclusive"] += 1
            continue

        if outcome.kind == "unresolvable":
            # Telegram has no record of this user in this chat at all: the
            # bot cannot act on this row, only the app can. Left open, never
            # counted as an access problem or a transient probe error.
            report.unresolvable.append((title, jr.user_id))
            stats["unresolvable"] += 1
            continue

        if outcome.kind == "alive":
            report.alive += 1
            stats["alive"] += 1
            continue

        if outcome.kind == "already_member":
            # Approved inside the Telegram app (or joined another way): the
            # request no longer exists. Closing the row is a database-only
            # change, nothing is sent to Telegram.
            if dry_run:
                report.already_member.append((title, jr.user_id))
                stats["already_member"] += 1
                continue
            closed = await _close_row(db, jr, DecisionEnum.approved, ALREADY_MEMBER_NOTE, {
                "purge_deleted": True, "already_member": True,
                "status": outcome.member_status, "actor": actor_name,
            }, now)
            if not closed:
                # A decision taken elsewhere while this sweep's probes were
                # running already resolved the row; it wins, and this sweep
                # never overwrites or double-logs it - but the row still
                # needs its own visible line, not silent disappearance. This
                # row was never part of deleted_found, so it gets its own
                # bucket, never the one deleted_found's breakdown must sum to.
                report.race_lost_already_member.append((title, jr.user_id))
                stats["race_lost_already_member"] += 1
                continue
            await _close_sessions(db, jr.user_id, g.chat_id, now, None)
            report.already_member.append((title, jr.user_id))
            stats["already_member"] += 1
            continue

        known_user = (await db.execute(
            select(TelegramUser).where(TelegramUser.telegram_id == jr.user_id)
        )).scalar_one_or_none()
        db_name = None
        if known_user:
            db_name = " ".join(
                part for part in (known_user.first_name, known_user.last_name) if part
            ) or None

        report.deleted_found.append((title, jr.user_id, db_name))
        stats["deleted_found"] += 1

        if dry_run:
            continue

        clear = outcome.clear
        if not clear.cleared:
            # Neither decline nor the ban fallback could clear it (access
            # problem, or both calls were refused): the request may still be
            # on Telegram, so the row stays open for a later retry.
            if clear.access_error:
                report.access_errors.append((title, clear.decline_error))
                error_text = clear.decline_error
            else:
                error_text = f"{clear.decline_error} | ban also failed: {clear.ban_error}"
            report.decline_failed.append((title, jr.user_id, error_text))
            stats["decline_failed"] += 1
            continue

        if clear.method == "decline":
            closed = await _close_row(db, jr, DecisionEnum.rejected, PURGE_NOTE, {
                "purge_deleted": True, "evidence": "empty first_name",
                "cleared_via": "decline", "actor": actor_name,
            }, now)
            if closed:
                await _close_sessions(db, jr.user_id, g.chat_id, now, False)
                report.removed.append((title, jr.user_id))
                stats["removed"] += 1
            else:
                # This row is already counted in deleted_found above, so it
                # belongs to that breakdown, never to race_lost_already_member.
                report.race_lost_deleted.append((title, jr.user_id))
                stats["race_lost_deleted"] += 1
        elif clear.method == "already_gone":
            closed = await _close_row(db, jr, DecisionEnum.rejected, PURGE_STALE_NOTE, {
                "purge_deleted": True, "evidence": "empty first_name",
                "cleared_via": "already_gone", "decline_error": clear.decline_error, "actor": actor_name,
            }, now)
            if closed:
                await _close_sessions(db, jr.user_id, g.chat_id, now, False)
                report.request_gone.append((title, jr.user_id))
                stats["request_gone"] += 1
            else:
                report.race_lost_deleted.append((title, jr.user_id))
                stats["race_lost_deleted"] += 1
        else:  # clear.method == "ban": decline was refused, the ban fallback cleared it
            # The ban itself already succeeded on Telegram regardless of
            # whether this call wins the race to close the local row, so it
            # is recorded unconditionally here - record_deleted_account_ban's
            # own upsert makes a repeat call for the same (user, group) a
            # no-op, never a duplicate row.
            await record_deleted_account_ban(
                db, g.id, jr.user_id, PURGE_BAN_NOTE, actor_name or "בוט (ניקוי בקשות)",
            )
            closed = await _close_row(db, jr, DecisionEnum.cleared_via_ban, PURGE_BAN_NOTE, {
                "purge_deleted": True, "evidence": "empty first_name",
                "cleared_via": "ban", "decline_error": clear.decline_error, "actor": actor_name,
            }, now)
            if closed:
                await _close_sessions(db, jr.user_id, g.chat_id, now, False)
                report.removed_via_ban.append((title, jr.user_id))
                stats["removed_via_ban"] += 1
            else:
                report.race_lost_deleted.append((title, jr.user_id))
                stats["race_lost_deleted"] += 1

    await db.flush()
    await db.commit()
    return report


# ─── Reconciliation sweep: close whatever a probe can prove ───────────────────

# The gap this closes: Telegram sends the bot an update when a join request is
# CREATED, and nothing else - an admin approving or declining inside the
# Telegram app, or the user withdrawing, produces no update at all. So an open
# JoinRequest row drifts further from reality the longer it sits, and the only
# way back is to ask Telegram directly about every user id the bot already
# holds. getChatMember proves membership and nothing else (see module docstring
# above purge_deleted_requests for the full rundown of what each call can and
# cannot tell us); it is read-only and safe to call for every row. The only
# destructive calls this sweep ever makes are declineChatJoinRequest and,
# when that is refused for a deactivated account, the banChatMember fallback
# in clear_deleted_account_request() - and, same rule as the purge above,
# only after the account is already proven deleted, never against a live
# applicant.

RECONCILE_ACTOR_LABEL = "בוט (בדיקת התאמה)"

RECON_JOINED_NOTE = "המשתמש נמצא כחבר בקבוצה, הבקשה הסתיימה מחוץ לבוט"
RECON_DELETED_NOTE = "חשבון מחוק, הבקשה נדחתה"
RECON_DELETED_STALE_NOTE = "חשבון מחוק, הבקשה לא היתה קיימת יותר בטלגרם"
RECON_DELETED_BAN_NOTE = "חשבון מחוק, טלגרם דחה את הסירוב (חשבון מושבת), הבקשה נוקתה באמצעות חסימה"
RECON_BANNED_NOTE = "המשתמש חסום או הוסר מהקבוצה מחוץ לבוט"
RECON_AUTO_CLOSE_NOTE = (
    "לא ניתן היה לקבוע את מצב הבקשה מול טלגרם, והיא נסגרה אוטומטית בגלל גיל "
    "של מעל {days} ימים. זו לא אישור ולא דחייה, רק סגירה כנה של בקשה לא ידועה."
)

RECONCILE_API_NOTE = (
    "ℹ️ הבדיקה הזו רואה רק בקשות שהבוט כבר מכיר, ואינה יכולה לדעת כמה בקשות "
    "ממתינות מופיעות כרגע באפליקציה של טלגרם. המספר שמופיע שם הוא המספר "
    "האמיתי. הבדיקה הזו סוגרת רק מה שהוכח בוודאות מול טלגרם."
)


@dataclass
class ReconciliationReport:
    checked: int = 0                                    # open rows the bot holds
    joined: list = field(default_factory=list)           # (title, user_id) now a member
    deleted: list = field(default_factory=list)           # (title, user_id) deleted account, declined
    deleted_request_gone: list = field(default_factory=list)   # (title, user_id) deleted, Telegram no longer had it
    deleted_via_ban: list = field(default_factory=list)   # (title, user_id) decline refused, cleared by a ban instead
    deleted_failed: list = field(default_factory=list)    # (title, user_id, error) deleted account, decline AND ban both failed, row left open
    banned: list = field(default_factory=list)            # (title, user_id) kicked outside the bot
    auto_closed_stale: list = field(default_factory=list)  # (title, user_id) too old, could not be proven
    unresolvable: list = field(default_factory=list)      # (title, user_id) Telegram has no record of this user in this chat; the bot cannot act, row left open
    race_lost: list = field(default_factory=list)         # (title, user_id) a decision elsewhere closed the row first; its own line, never silently dropped
    undetermined: int = 0                                 # left open, nothing provable
    access_errors: list = field(default_factory=list)     # (title, error_string)
    stale_skipped_groups: list = field(default_factory=list)  # titles: group had a probe error, so no stale-closing ran there this run
    per_group: dict = field(default_factory=dict)         # keyed by Group.id, carries "title"
    member_counts: dict = field(default_factory=dict)     # keyed by Group.id, Telegram's count or None
    stopped_early: bool = False


@dataclass
class _ReconOutcome:
    kind: str                          # joined | deleted | banned | undetermined | unresolvable
    access_error: Optional[str] = None
    member_status: Optional[str] = None
    clear: Optional[ClearOutcome] = None
    # True whenever this outcome came from a probe failure (an access error,
    # a network error, an exhausted 429, or any other exception) rather than
    # a genuine answer from Telegram. Never eligible for the stale-close
    # rule below - only a real "left"/"not a member" status from a
    # successful getChatMember call is.
    is_error: bool = False


def _row_age(created_at: Optional[datetime], now: datetime) -> Optional[timedelta]:
    """Returns None when the row's age cannot be determined at all (no
    created_at). An unknown age is a deliberate exemption from the stale
    rule, not an assumption that the row is either very old or very fresh -
    see the stale-eligibility check in run_reconciliation_sweep."""
    if created_at is None:
        return None
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
    return now - created_at


async def _probe_reconciliation_row(bot: Bot, chat_id: int, user_id: int, dry_run: bool) -> _ReconOutcome:
    """Telegram side of one row only, same isolation rule as _probe_row above:
    no database access here, rows are probed concurrently.

    Only two kinds of outcome count as genuine evidence the stale rule may
    act on: "joined"/"banned" (decisive either way) and an undetermined
    outcome carrying a real member_status straight from a successful
    getChatMember call (typically "left"). Every failure path - an access
    error, the get_chat fallback also failing, an exhausted 429, a network
    error, or any other exception - is marked is_error=True so a transient
    probe failure is never mistaken for proof the user left the group."""
    try:
        member = await _call_honouring_flood(lambda: bot.get_chat_member(chat_id, user_id))
    except TelegramAPIError as e:
        err = str(e)
        if any(marker in err.lower() for marker in _ACCESS_ERROR_MARKERS):
            return _ReconOutcome("undetermined", access_error=err, is_error=True)

        await asyncio.sleep(0.05)
        try:
            chat_info = await _call_honouring_flood(lambda: bot.get_chat(user_id))
        except TelegramAPIError:
            if any(marker in err.lower() for marker in _UNRESOLVABLE_MARKERS):
                # Telegram has no record of this user in this chat at all -
                # not an access problem, not proof of deletion, and not a
                # transient probe error: never poison the group's stale rule
                # or the daily notification just because this row exists.
                return _ReconOutcome("unresolvable")
            return _ReconOutcome("undetermined", is_error=True)

        if not _looks_empty(chat_info.first_name, chat_info.username, chat_info.last_name):
            # Account is alive, but the membership probe itself failed for a
            # non-access reason: that proves nothing about membership, so it
            # is still an error for stale purposes, not a genuine answer.
            return _ReconOutcome("undetermined", is_error=True)
    else:
        user = member.user
        if _looks_empty(user.first_name, user.username, user.last_name):
            pass  # deleted account: the decline below is the decisive call
        elif _is_member(member):
            return _ReconOutcome("joined", member_status=member.status)
        elif member.status == "kicked":
            return _ReconOutcome("banned", member_status=member.status)
        else:
            # getChatMember answered directly with a real, non-member status
            # (typically "left") - the only genuine evidence the stale rule
            # may act on.
            return _ReconOutcome("undetermined", member_status=member.status)

    outcome = _ReconOutcome("deleted")
    if dry_run:
        return outcome

    outcome.clear = await clear_deleted_account_request(bot, chat_id, user_id)
    return outcome


async def run_reconciliation_sweep(
    bot: Bot,
    *,
    group_id: Optional[int] = None,
    dry_run: bool = True,
    stale_days: Optional[int] = None,
    actor_name: str = RECONCILE_ACTOR_LABEL,
    db: AsyncSession = None,
    max_probes: int = 2000,
    concurrency: int = PURGE_CONCURRENCY,
    progress: Optional[ProgressCallback] = None,
    progress_interval: float = 3.0,
) -> ReconciliationReport:
    """Walks every open join_requests row (of one group, or every active
    group) and closes exactly what a Telegram probe can prove: the user is now
    a member, the account is deleted, or the user was kicked/removed outside
    the bot. A row that stays undetermined is kept open unless it is already
    older than stale_days AND the probe gave a genuine non-member answer
    (never an error) AND the row's age is actually known AND no other row in
    the same group hit a probe error this run - in which case it is closed as
    an honest "could not tell" outcome, never as an approval or rejection.

    stale_days=0 (the config default - see config/settings.py) disables that
    auto-close entirely, by policy: an undetermined row might still be a
    genuine pending request the app still shows, and auto-closing it by age
    alone is exactly the silent-loss failure the owner reported. An admin who
    explicitly wants age-based auto-close for undetermined rows can still opt
    into it by setting reconciliation_stale_days above 0; this parameter
    lets a caller override that per call regardless of the config value.
    An "unresolvable" row (Telegram has no record of this user in this chat
    at all) is reported separately and is never eligible for this rule
    either way - see _probe_reconciliation_row.

    Every closing write is conditional on the row still being open, so a
    decision made elsewhere (a manual approve/reject/ban) while the probes
    were running always wins and the same row is never closed twice."""
    if stale_days is None:
        from config import settings as _settings
        stale_days = _settings.reconciliation_stale_days

    report = ReconciliationReport()
    now = datetime.now(timezone.utc)

    group_query = select(Group).where(Group.is_active, ~Group.is_banned)
    if group_id is not None:
        group_query = group_query.where(Group.id == group_id)
    groups = (await db.execute(group_query)).scalars().all()

    # Phase 1 (database + one Telegram call per group): collect every open
    # row up to the probe budget, plus the group's member count for context -
    # never the group's pending count, which Telegram has no API to read.
    rows: list[tuple[Group, JoinRequest]] = []
    for g in groups:
        report.per_group.setdefault(g.id, {
            "title": g.title or str(g.chat_id),
            "checked": 0, "joined": 0, "deleted": 0, "deleted_request_gone": 0,
            "deleted_via_ban": 0, "deleted_failed": 0, "banned": 0,
            "auto_closed_stale": 0, "unresolvable": 0, "race_lost": 0,
            "undetermined": 0, "access_error": None,
        })
        try:
            report.member_counts[g.id] = await _call_honouring_flood(
                lambda chat_id=g.chat_id: bot.get_chat_member_count(chat_id)
            )
        except TelegramAPIError:
            report.member_counts[g.id] = None

        jr_result = await db.execute(select(JoinRequest).where(
            JoinRequest.group_id == g.id,
            JoinRequest.decision.in_((DecisionEnum.pending, DecisionEnum.manual_review)),
            JoinRequest.resolved_at.is_(None),
        ))
        for jr in jr_result.scalars().all():
            if len(rows) >= max_probes:
                report.stopped_early = True
                break
            rows.append((g, jr))
        if report.stopped_early:
            break

    # Phase 2 (Telegram): probe in parallel with bounded concurrency.
    total = len(rows)
    semaphore = asyncio.Semaphore(max(1, concurrency))
    done = 0
    last_progress = time.monotonic()

    async def probe(g: Group, jr: JoinRequest) -> _ReconOutcome:
        nonlocal done, last_progress
        async with semaphore:
            try:
                outcome = await _probe_reconciliation_row(bot, g.chat_id, jr.user_id, dry_run)
            except Exception:  # never let one row sink the whole sweep; the row stays open
                logger.exception(f"reconcile: probe failed for {jr.user_id} in {g.chat_id}")
                outcome = _ReconOutcome("undetermined", is_error=True)
            await asyncio.sleep(0.05)
        done += 1
        if progress is not None and time.monotonic() - last_progress >= progress_interval:
            last_progress = time.monotonic()
            try:
                await progress(done, total)
            except Exception:
                logger.exception("reconcile: progress callback failed")
        return outcome

    outcomes = await asyncio.gather(*(probe(g, jr) for g, jr in rows))

    # A probe error anywhere in a group poisons the stale rule for the whole
    # group this run: one network blip or one kick must never be allowed to
    # mass-close every other aged row in that group as stale. A deleted
    # account that neither decline nor the ban fallback could clear (access,
    # flood, network, or a refusal on both calls) is just as much a probe
    # error as an undetermined outcome: it stays open, so it must poison the
    # group's stale rule too. A deleted account cleared via the ban fallback
    # is real evidence, not an error, and never poisons anything.
    #
    # Skipped entirely when the stale-close rule itself is off (stale_days
    # <= 0): there is nothing to poison, and reporting "stale skipped"
    # groups when the rule is off is misleading - it is also what let a
    # group stuck behind a permanent probe error trigger the daily loop's
    # owner notification forever, even with the rule disabled.
    groups_with_errors: set = set()
    if stale_days and stale_days > 0:
        groups_with_errors = {
            g.id for (g, jr), outcome in zip(rows, outcomes)
            if (outcome.kind == "undetermined" and outcome.is_error)
            or (outcome.kind == "deleted" and outcome.clear is not None and not outcome.clear.cleared)
        }
        for gid in groups_with_errors:
            title = report.per_group[gid]["title"]
            if title not in report.stale_skipped_groups:
                report.stale_skipped_groups.append(title)

    # Phase 3 (database): apply the outcomes sequentially, in row order.
    for (g, jr), outcome in zip(rows, outcomes):
        title = g.title or str(g.chat_id)
        stats = report.per_group[g.id]
        report.checked += 1
        stats["checked"] += 1

        if outcome.kind == "unresolvable":
            # Telegram has no record of this user in this chat at all: the
            # bot cannot act on this row, only the app can. Reported on its
            # own, left open, and never counted toward the stale rule or the
            # group-error poisoning above.
            report.unresolvable.append((title, jr.user_id))
            stats["unresolvable"] += 1
            continue

        if outcome.kind == "undetermined":
            if outcome.access_error and stats["access_error"] is None:
                stats["access_error"] = outcome.access_error
                report.access_errors.append((title, outcome.access_error))

            if outcome.is_error:
                # A probe failure is never evidence the user left the group -
                # the row stays open exactly like any other undetermined row.
                report.undetermined += 1
                stats["undetermined"] += 1
                continue

            row_age = _row_age(jr.created_at, now)
            if (
                stale_days and stale_days > 0
                and g.id not in groups_with_errors
                and row_age is not None
                and row_age >= timedelta(days=stale_days)
            ):
                if dry_run:
                    report.auto_closed_stale.append((title, jr.user_id))
                    stats["auto_closed_stale"] += 1
                    continue
                closed = await _close_row(
                    db, jr, DecisionEnum.stale, RECON_AUTO_CLOSE_NOTE.format(days=stale_days), {
                        "reconciliation": True, "reason": "auto_closed_stale",
                        "stale_days": stale_days, "actor": actor_name,
                    }, now,
                )
                if not closed:
                    # A decision taken elsewhere while the probes were
                    # running already resolved the row; it wins, but this
                    # row still needs its own visible line.
                    report.race_lost.append((title, jr.user_id))
                    stats["race_lost"] += 1
                    continue
                jr.reviewed_by = actor_name
                report.auto_closed_stale.append((title, jr.user_id))
                stats["auto_closed_stale"] += 1
                continue

            report.undetermined += 1
            stats["undetermined"] += 1
            continue

        if outcome.kind == "joined":
            if dry_run:
                report.joined.append((title, jr.user_id))
                stats["joined"] += 1
                continue
            closed = await _close_row(db, jr, DecisionEnum.approved, RECON_JOINED_NOTE, {
                "reconciliation": True, "reason": "already_member",
                "status": outcome.member_status, "actor": actor_name,
            }, now)
            if not closed:
                report.race_lost.append((title, jr.user_id))
                stats["race_lost"] += 1
                continue
            jr.reviewed_by = actor_name
            await _close_sessions(db, jr.user_id, g.chat_id, now, None)
            report.joined.append((title, jr.user_id))
            stats["joined"] += 1
            continue

        if outcome.kind == "banned":
            if dry_run:
                report.banned.append((title, jr.user_id))
                stats["banned"] += 1
                continue
            closed = await _close_row(db, jr, DecisionEnum.banned, RECON_BANNED_NOTE, {
                "reconciliation": True, "reason": "kicked_outside_bot",
                "status": outcome.member_status, "actor": actor_name,
            }, now)
            if not closed:
                report.race_lost.append((title, jr.user_id))
                stats["race_lost"] += 1
                continue
            jr.reviewed_by = actor_name
            await _close_sessions(db, jr.user_id, g.chat_id, now, False)
            report.banned.append((title, jr.user_id))
            stats["banned"] += 1
            continue

        # outcome.kind == "deleted"
        if dry_run:
            report.deleted.append((title, jr.user_id))
            stats["deleted"] += 1
            continue

        clear = outcome.clear
        if not clear.cleared:
            # Neither decline nor the ban fallback could clear it: the
            # request may still be on Telegram, so the row stays open for a
            # later retry (groups_with_errors above already poisons the
            # stale rule for this group because of this exact row). This is
            # a clearing FAILURE, not a plain undetermined row - it gets its
            # own line so it is never mistaken for "nothing provable".
            if clear.access_error:
                report.access_errors.append((title, clear.decline_error))
                error_text = clear.decline_error
            else:
                error_text = f"{clear.decline_error} | ban also failed: {clear.ban_error}"
            report.deleted_failed.append((title, jr.user_id, error_text))
            stats["deleted_failed"] += 1
            continue

        if clear.method == "already_gone":
            closed = await _close_row(db, jr, DecisionEnum.rejected, RECON_DELETED_STALE_NOTE, {
                "reconciliation": True, "reason": "deleted_account_request_gone",
                "decline_error": clear.decline_error, "actor": actor_name,
            }, now)
            if closed:
                jr.reviewed_by = actor_name
                await _close_sessions(db, jr.user_id, g.chat_id, now, False)
                report.deleted_request_gone.append((title, jr.user_id))
                stats["deleted_request_gone"] += 1
            else:
                report.race_lost.append((title, jr.user_id))
                stats["race_lost"] += 1
            continue

        if clear.method == "ban":
            # The ban itself already succeeded on Telegram regardless of
            # whether this call wins the race to close the local row, so it
            # is recorded unconditionally here - record_deleted_account_ban's
            # own upsert makes a repeat call for the same (user, group) a
            # no-op, never a duplicate row.
            await record_deleted_account_ban(db, g.id, jr.user_id, RECON_DELETED_BAN_NOTE, actor_name)
            closed = await _close_row(db, jr, DecisionEnum.cleared_via_ban, RECON_DELETED_BAN_NOTE, {
                "reconciliation": True, "reason": "deleted_account_cleared_via_ban",
                "decline_error": clear.decline_error, "actor": actor_name,
            }, now)
            if closed:
                jr.reviewed_by = actor_name
                await _close_sessions(db, jr.user_id, g.chat_id, now, False)
                report.deleted_via_ban.append((title, jr.user_id))
                stats["deleted_via_ban"] += 1
            else:
                report.race_lost.append((title, jr.user_id))
                stats["race_lost"] += 1
            continue

        closed = await _close_row(db, jr, DecisionEnum.rejected, RECON_DELETED_NOTE, {
            "reconciliation": True, "reason": "deleted_account",
            "decline_error": None, "actor": actor_name,
        }, now)
        if closed:
            jr.reviewed_by = actor_name
            await _close_sessions(db, jr.user_id, g.chat_id, now, False)
            report.deleted.append((title, jr.user_id))
            stats["deleted"] += 1
        else:
            report.race_lost.append((title, jr.user_id))
            stats["race_lost"] += 1

    await db.flush()
    await db.commit()
    return report
