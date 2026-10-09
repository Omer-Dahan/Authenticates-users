"""Manual user-ban commands: /ban, /unban, /bans (access path 1).

The actual ban/unban logic lives in moderation/actions.py and is shared with
the manual-review button, the super-admin panel, and the terminal script.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import Group, TelegramUser, UserBan
from moderation.actions import ban_user, unban_user, BanReport, VALID_SCOPES
from bot.keyboards.settings_kb import ban_scope_picker_kb, bans_list_kb
from config import settings
from logs import get_logger

logger = get_logger(__name__)
router = Router()

SCOPE_LABELS = {
    "global": "חסימה גלובלית (כל הקבוצות)",
    "global_remove": "הסרה גלובלית, חסימה מקומית",
    "local": "חסימה מקומית",
}

# ban:scope callback_data only has room for id + scope, not free text, so the
# reason typed into /ban is parked here until the scope button is pressed.
_PENDING_BAN_REASONS: dict[tuple[int, int], dict] = {}
_PENDING_REASON_TTL = timedelta(minutes=10)


def _store_pending_reason(admin_id: int, target_id: int, reason: Optional[str]) -> None:
    now = datetime.now(timezone.utc)
    expired = [key for key, entry in _PENDING_BAN_REASONS.items() if now - entry["created_at"] > _PENDING_REASON_TTL]
    for key in expired:
        del _PENDING_BAN_REASONS[key]

    if reason is None:
        return
    _PENDING_BAN_REASONS[(admin_id, target_id)] = {
        "reason": reason,
        "created_at": now,
    }


def _pop_pending_reason(admin_id: int, target_id: int) -> Optional[str]:
    entry = _PENDING_BAN_REASONS.pop((admin_id, target_id), None)
    if entry is None:
        return None
    if datetime.now(timezone.utc) - entry["created_at"] > _PENDING_REASON_TTL:
        return None
    return entry["reason"]


def _is_super_admin(user_id: int) -> bool:
    return user_id == settings.super_admin_id


def format_ban_report(report: BanReport) -> str:
    lines = []
    if report.banned_in:
        lines.append("🔨 נחסם בקבוצות: " + ", ".join(report.banned_in))
    if report.removed_from:
        lines.append("🚪 הוסר מהקבוצות: " + ", ".join(report.removed_from))
    if report.failed:
        failed_str = "\n".join(f"  - {title}: {err}" for title, err in report.failed)
        lines.append("⚠️ כשל בקבוצות:\n" + failed_str)
    if report.skipped:
        lines.append("⏭ קבוצות שדולגו: " + ", ".join(report.skipped))
    return "\n".join(lines) if lines else "לא בוצעה פעולה באף קבוצה."


async def _resolve_target(identifier: str, db) -> Optional[int]:
    """Resolves a telegram_id or @username to a telegram_id. A numeric id always works."""
    identifier = identifier.strip()
    if identifier.startswith("@"):
        result = await db.execute(
            select(TelegramUser).where(TelegramUser.username == identifier[1:])
        )
        user = result.scalar_one_or_none()
        return user.telegram_id if user else None
    try:
        return int(identifier)
    except ValueError:
        return None


@router.message(Command("ban"))
async def cmd_ban(message: Message, bot: Bot) -> None:
    parts = message.text.split(maxsplit=2)
    if len(parts) < 2:
        await message.reply("שימוש: /ban <telegram_id|@username> [סיבה]")
        return

    identifier = parts[1]
    reason = parts[2] if len(parts) > 2 else None

    async with AsyncSessionLocal() as db:
        telegram_id = await _resolve_target(identifier, db)
        if telegram_id is None:
            await message.reply(f"❌ לא נמצא משתמש עבור {identifier}.")
            return

        if _is_super_admin(message.from_user.id):
            _store_pending_reason(message.from_user.id, telegram_id, reason)
            await message.reply(
                f"בחר את סוג החסימה עבור <code>{telegram_id}</code>:",
                parse_mode="HTML",
                reply_markup=ban_scope_picker_kb(telegram_id),
            )
            return

        # Not super-admin: only a group owner, acting inside their own group, may ban - and only locally.
        result = await db.execute(select(Group).where(Group.chat_id == message.chat.id))
        group = result.scalar_one_or_none()
        if not group or group.owner_id != message.from_user.id:
            await message.reply("⛔ אין לך הרשאה לבצע חסימה.")
            return

        report = await ban_user(
            bot, telegram_id, "local",
            group_id=group.id, reason=reason,
            actor_id=message.from_user.id,
            actor_name=message.from_user.username or str(message.from_user.id),
            db=db,
        )

    await message.reply(
        f"🔨 <b>המשתמש נחסם ({SCOPE_LABELS['local']})</b>\n\n{format_ban_report(report)}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("ban:scope:"))
async def cb_ban_scope(callback: CallbackQuery, bot: Bot) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer("⛔ אין הרשאה.", show_alert=True)
        return

    parts = callback.data.split(":")
    if len(parts) != 4:
        await callback.answer("כפתור לא תקין.", show_alert=True)
        return

    try:
        telegram_id = int(parts[2])
    except ValueError:
        await callback.answer("כפתור לא תקין.", show_alert=True)
        return

    scope = parts[3]
    if scope not in VALID_SCOPES:
        await callback.answer("כפתור לא תקין או שפג תוקפו.", show_alert=True)
        return

    reason = _pop_pending_reason(callback.from_user.id, telegram_id)

    async with AsyncSessionLocal() as db:
        group = None
        if scope in ("local", "global_remove"):
            result = await db.execute(select(Group).where(Group.chat_id == callback.message.chat.id))
            group = result.scalar_one_or_none()
            if not group:
                await callback.answer(
                    "לא נמצאה קבוצה בצ'אט הנוכחי. יש להשתמש ב-/ban בתוך הקבוצה הרצויה.",
                    show_alert=True,
                )
                return

        report = await ban_user(
            bot, telegram_id, scope,
            group_id=group.id if group else None,
            reason=reason,
            actor_id=callback.from_user.id,
            actor_name=callback.from_user.username or str(callback.from_user.id),
            db=db,
        )

    await callback.answer("בוצע.")
    try:
        await callback.message.edit_text(
            f"🔨 <b>המשתמש נחסם ({SCOPE_LABELS.get(scope, scope)})</b>\n\n{format_ban_report(report)}",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass


@router.message(Command("unban"))
async def cmd_unban(message: Message, bot: Bot) -> None:
    if not _is_super_admin(message.from_user.id):
        await message.reply("⛔ אין לך הרשאה לבצע פעולה זו.")
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("שימוש: /unban <telegram_id|@username>")
        return

    async with AsyncSessionLocal() as db:
        telegram_id = await _resolve_target(parts[1], db)
        if telegram_id is None:
            await message.reply(f"❌ לא נמצא משתמש עבור {parts[1]}.")
            return

        report = await unban_user(
            bot, telegram_id,
            actor_id=message.from_user.id,
            actor_name=message.from_user.username or str(message.from_user.id),
            db=db,
        )

    await message.reply(f"✅ <b>החסימה הוסרה</b>\n\n{format_ban_report(report)}", parse_mode="HTML")


async def _active_bans(db):
    result = await db.execute(
        select(UserBan).where(UserBan.lifted_at.is_(None)).order_by(UserBan.created_at.desc())
    )
    return result.scalars().all()


@router.message(Command("bans"))
async def cmd_bans(message: Message) -> None:
    if not _is_super_admin(message.from_user.id):
        return

    async with AsyncSessionLocal() as db:
        bans = await _active_bans(db)

    if not bans:
        await message.reply("אין חסימות פעילות.")
        return

    await message.reply(
        f"🚫 <b>חסימות פעילות ({len(bans)})</b>",
        parse_mode="HTML",
        reply_markup=bans_list_kb(bans),
    )


@router.callback_query(F.data.startswith("ban:lift:"))
async def cb_ban_lift(callback: CallbackQuery, bot: Bot) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer("⛔ אין הרשאה.", show_alert=True)
        return

    ban_id = int(callback.data.split(":")[2])

    async with AsyncSessionLocal() as db:
        ban_row = await db.get(UserBan, ban_id)
        if not ban_row or ban_row.lifted_at is not None:
            await callback.answer("החסימה כבר הוסרה.")
            return

        await unban_user(
            bot, ban_row.telegram_id,
            ban_id=ban_row.id,
            actor_id=callback.from_user.id,
            actor_name=callback.from_user.username or str(callback.from_user.id),
            db=db,
        )
        remaining = await _active_bans(db)

    await callback.answer("החסימה הוסרה.", show_alert=True)
    try:
        if remaining:
            await callback.message.edit_text(
                f"🚫 <b>חסימות פעילות ({len(remaining)})</b>",
                parse_mode="HTML",
                reply_markup=bans_list_kb(remaining),
            )
        else:
            await callback.message.edit_text("אין חסימות פעילות.", parse_mode="HTML")
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("ban:noop"))
async def cb_ban_noop(callback: CallbackQuery) -> None:
    await callback.answer()
