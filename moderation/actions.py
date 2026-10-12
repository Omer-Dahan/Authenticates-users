"""Shared ban/unban service used by every manual-ban access path: commands, the
manual-review button, the super-admin panel, and the terminal script.
"""
import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramRetryAfter
from sqlalchemy import select, or_
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
) -> Optional[UserBan]:
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
            UserBan.group_id == effective_group_id,
        )
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
#   required field for every live account.
# - A probe that resolves with a member status proves the request is gone: a
#   member cannot have a pending request for the same chat.
# - declineChatJoinRequest is the only decisive call for "does the request still
#   exist": True means it existed and is now declined, HIDE_REQUESTER_MISSING
#   means Telegram no longer has it. It is only ever sent for deleted accounts,
#   so a live applicant is never declined by this purge.
# These two error substrings mean an access problem (group access lost, or the
# bot lacks admin rights), never "the user is gone": never infer deletion from
# them, and never even attempt the fallback probe.
_ACCESS_ERROR_MARKERS = ("not enough rights", "chat not found")
_REQUEST_MISSING_MARKER = "hide_requester_missing"
_MEMBER_STATUSES = ("member", "administrator", "creator")

PURGE_NOTE = "account deleted (empty profile)"
PURGE_STALE_NOTE = "account deleted, request no longer on Telegram"
ALREADY_MEMBER_NOTE = "already a member, request resolved outside the bot"


@dataclass
class PurgeReport:
    checked: int = 0                                     # open rows the bot holds
    deleted_found: list = field(default_factory=list)   # (title, user_id, name_from_db)
    removed: list = field(default_factory=list)          # (title, user_id) declined, Telegram still had it
    request_gone: list = field(default_factory=list)     # (title, user_id) deleted, Telegram no longer had it
    already_member: list = field(default_factory=list)   # (title, user_id) request resolved outside the bot
    decline_failed: list = field(default_factory=list)   # (title, user_id, error) row left open
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


def _close_row(db: AsyncSession, jr: JoinRequest, decision: DecisionEnum, note: str,
               details: dict, now: datetime) -> None:
    jr.decision = decision
    jr.resolved_at = now
    jr.notes = note
    db.add(ModerationLog(
        group_id=jr.group_id,
        join_request_id=jr.id,
        user_id=jr.user_id,
        decision=decision,
        score=jr.score,
        matched_rules=jr.matched_rules,
        details=details,
    ))


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
class _PurgeOutcome:
    kind: str                          # deleted | already_member | alive | inconclusive
    access_error: Optional[str] = None
    member_status: Optional[str] = None
    decline_error: Optional[str] = None


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
            return _PurgeOutcome("inconclusive")

        if not _looks_empty(chat_info.first_name, chat_info.username, chat_info.last_name):
            return _PurgeOutcome("alive")
    else:
        user = member.user
        if _looks_empty(user.first_name, user.username, user.last_name):
            pass  # deleted account: the decline below is the decisive call
        elif _is_member(member):
            return _PurgeOutcome("already_member", member_status=member.status)
        else:
            return _PurgeOutcome("alive")

    outcome = _PurgeOutcome("deleted")
    if dry_run:
        return outcome

    try:
        await _call_honouring_flood(lambda: bot.decline_chat_join_request(chat_id, user_id))
    except TelegramAPIError as e:
        outcome.decline_error = str(e)
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
            "already_member": 0, "decline_failed": 0,
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

        if outcome.kind == "alive":
            report.alive += 1
            stats["alive"] += 1
            continue

        if outcome.kind == "already_member":
            # Approved inside the Telegram app (or joined another way): the
            # request no longer exists. Closing the row is a database-only
            # change, nothing is sent to Telegram.
            report.already_member.append((title, jr.user_id))
            stats["already_member"] += 1
            if not dry_run:
                _close_row(db, jr, DecisionEnum.approved, ALREADY_MEMBER_NOTE, {
                    "purge_deleted": True, "already_member": True,
                    "status": outcome.member_status, "actor": actor_name,
                }, now)
                await _close_sessions(db, jr.user_id, g.chat_id, now, None)
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

        decline_error = outcome.decline_error
        if decline_error is not None:
            if is_request_missing_error(decline_error):
                report.request_gone.append((title, jr.user_id))
                stats["request_gone"] += 1
                _close_row(db, jr, DecisionEnum.rejected, PURGE_STALE_NOTE, {
                    "purge_deleted": True, "evidence": "empty first_name",
                    "decline_error": decline_error, "actor": actor_name,
                }, now)
                await _close_sessions(db, jr.user_id, g.chat_id, now, False)
            else:
                # Nothing decisive came back (access, flood, network): the
                # request may still be on Telegram, so the row stays open.
                if any(marker in decline_error.lower() for marker in _ACCESS_ERROR_MARKERS):
                    report.access_errors.append((title, decline_error))
                report.decline_failed.append((title, jr.user_id, decline_error))
                stats["decline_failed"] += 1
            continue

        report.removed.append((title, jr.user_id))
        stats["removed"] += 1
        _close_row(db, jr, DecisionEnum.rejected, PURGE_NOTE, {
            "purge_deleted": True, "evidence": "empty first_name",
            "decline_error": None, "actor": actor_name,
        }, now)
        await _close_sessions(db, jr.user_id, g.chat_id, now, False)

    await db.flush()
    await db.commit()
    return report
