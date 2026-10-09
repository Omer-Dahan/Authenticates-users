"""Shared ban/unban service used by every manual-ban access path: commands, the
manual-review button, the super-admin panel, and the terminal script.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
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
