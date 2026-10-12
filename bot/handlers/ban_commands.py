"""Manual user-ban commands: /ban, /unban, /bans (access path 1).

The actual ban/unban logic lives in moderation/actions.py and is shared with
the manual-review button, the super-admin panel, and the terminal script.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import Group, TelegramUser, UserBan
from moderation.actions import ban_user, unban_user, BanReport, VALID_SCOPES, purge_deleted_requests, PurgeReport
from bot.keyboards.settings_kb import ban_scope_picker_kb, bans_list_kb
from bot.text import esc
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


# The purge confirm button must act on exactly the dry run the admin saw, not
# on whatever the callback data claims - so the dry run's scope is parked
# server-side, keyed by admin, and consumed (one-shot) by the confirm press.
_PENDING_PURGES: dict[int, dict] = {}
_PENDING_PURGE_TTL = timedelta(minutes=10)


def _store_pending_purge(admin_id: int, group_id: Optional[int]) -> None:
    now = datetime.now(timezone.utc)
    expired = [key for key, entry in _PENDING_PURGES.items() if now - entry["created_at"] > _PENDING_PURGE_TTL]
    for key in expired:
        del _PENDING_PURGES[key]

    _PENDING_PURGES[admin_id] = {"group_id": group_id, "created_at": now}


def _pop_pending_purge(admin_id: int) -> Optional[dict]:
    entry = _PENDING_PURGES.pop(admin_id, None)
    if entry is None:
        return None
    if datetime.now(timezone.utc) - entry["created_at"] > _PENDING_PURGE_TTL:
        return None
    return entry


def _is_super_admin(user_id: int) -> bool:
    return user_id == settings.super_admin_id


def format_ban_report(report: BanReport) -> str:
    lines = []
    if report.banned_in:
        lines.append("🔨 נחסם בקבוצות: " + ", ".join(esc(t) for t in report.banned_in))
    if report.removed_from:
        lines.append("🚪 הוסר מהקבוצות: " + ", ".join(esc(t) for t in report.removed_from))
    if report.failed:
        failed_str = "\n".join(f"  - {esc(title)}: {esc(err)}" for title, err in report.failed)
        lines.append("⚠️ כשל בקבוצות:\n" + failed_str)
    if report.skipped:
        lines.append("⏭ קבוצות שדולגו: " + ", ".join(esc(t) for t in report.skipped))
    return "\n".join(lines) if lines else "לא בוצעה פעולה באף קבוצה."


PURGE_API_NOTE = (
    "ℹ️ טלגרם לא מאפשר לבוט לראות את רשימת הבקשות הממתינות, ולא מעדכן אותו "
    "כשבקשה מאושרת או נדחית באפליקציה או כשהמשתמש מבטל אותה. לכן השורות כאן הן "
    "בקשות שהבוט ראה פעם, והמספר שמופיע באפליקציה הוא המספר האמיתי. "
    "רק חשבונות מחוקים נדחים; כל השאר לא נוגעים בהם."
)


def format_purge_report(report: PurgeReport, applied: bool) -> str:
    lines = [f"📦 בקשות פתוחות במסד הנתונים של הבוט: {report.checked}"]
    if report.deleted_found:
        lines.append(f"🗑 חשבונות מחוקים: {len(report.deleted_found)}")
        for title, uid, name in report.deleted_found[:20]:
            name_display = esc(name) if name else esc(str(uid))
            lines.append(f"  • {name_display} (<code>{uid}</code>) בקבוצה {esc(title)}")
    else:
        lines.append("🗑 לא נמצאו חשבונות מחוקים.")
    if applied:
        lines.append(f"✅ טלגרם עדיין הכיר את הבקשה, ונדחתה: {len(report.removed)}")
        lines.append(f"👻 טלגרם כבר לא הכיר את הבקשה, נסגרה אצל הבוט: {len(report.request_gone)}")
        lines.append(f"👥 כבר חברים בקבוצה, הבקשה נסגרה אצל הבוט: {len(report.already_member)}")
        if report.decline_failed:
            lines.append(f"⚠️ הדחייה נכשלה, השורה נשארה פתוחה: {len(report.decline_failed)}")
    else:
        if report.deleted_found:
            lines.append("  רק בלחיצת האישור יתברר מול טלגרם אם הבקשות האלה עדיין קיימות.")
        lines.append(f"👥 כבר חברים בקבוצה (טופלו מחוץ לבוט): {len(report.already_member)}")
    lines.append(f"❔ החשבון קיים, לא ידוע אם הבקשה עדיין ממתינה: {report.alive}")
    lines.append(f"❓ טלגרם לא החזיר מידע, לא ידוע אם הבקשה קיימת: {report.inconclusive}")
    if report.access_errors:
        lines.append("⚠️ בעיית גישה:")
        seen = set()
        for title, err in report.access_errors:
            if title in seen:
                continue
            seen.add(title)
            lines.append(f"  • {esc(title)}: {esc(err)}")
    if report.stopped_early:
        lines.append(f"⏹ הבדיקה נעצרה אחרי {report.checked} בדיקות (הגעה למגבלה).")

    if report.per_group:
        lines.append("")
        lines.append("📍 <b>פירוט לפי קבוצה:</b>")
        for key, stats in report.per_group.items():
            lines.append(f"\n<b>{esc(stats.get('title', key))}</b>")
            lines.append(
                f"  נבדקו: {stats['checked']} | מחוקים: {stats['deleted_found']} | "
                f"כבר חברים: {stats.get('already_member', 0)} | "
                f"לא ידוע: {stats['alive'] + stats['inconclusive']}"
            )
            if applied:
                lines.append(
                    f"  נדחו: {stats['removed']} | כבר לא בטלגרם: {stats.get('request_gone', 0)}"
                )
            if stats.get("access_error"):
                lines.append(f"  ⚠️ שגיאת גישה: {esc(stats['access_error'])}")

    lines.append("")
    lines.append(PURGE_API_NOTE)
    return "\n".join(lines)


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
        await message.reply("שימוש: /ban &lt;telegram_id|@username&gt; [סיבה]")
        return

    identifier = parts[1]
    reason = parts[2] if len(parts) > 2 else None

    async with AsyncSessionLocal() as db:
        telegram_id = await _resolve_target(identifier, db)
        if telegram_id is None:
            # identifier is free text typed by the admin - it reaches a reply
            # sent with the bot's default HTML parse mode, so any "<"/">" in
            # it (e.g. a stray "/ban <@name>") must be escaped or Telegram
            # rejects the whole message as an unsupported start tag, which
            # crashes this update with no feedback to the admin.
            await message.reply(f"❌ לא נמצא משתמש עבור {esc(identifier)}.")
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
        await message.reply("שימוש: /unban &lt;telegram_id|@username&gt;")
        return

    async with AsyncSessionLocal() as db:
        telegram_id = await _resolve_target(parts[1], db)
        if telegram_id is None:
            await message.reply(f"❌ לא נמצא משתמש עבור {esc(parts[1])}.")
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


# ─── /purge_deleted: pending requests from deleted accounts (access path 1) ───

@router.message(Command("purge_deleted"))
async def cmd_purge_deleted(message: Message, bot: Bot) -> None:
    if not _is_super_admin(message.from_user.id):
        await message.reply("⛔ אין לך הרשאה לבצע פעולה זו.")
        return

    parts = message.text.split(maxsplit=1)
    target_group_id = None

    async with AsyncSessionLocal() as db:
        if len(parts) > 1:
            try:
                chat_id_arg = int(parts[1].strip())
            except ValueError:
                await message.reply("❌ יש לציין chat_id מספרי.")
                return
            result = await db.execute(select(Group).where(Group.chat_id == chat_id_arg))
            group = result.scalar_one_or_none()
            if not group:
                await message.reply(f"❌ לא נמצאה קבוצה עם chat_id {chat_id_arg}.")
                return
            target_group_id = group.id

        report = await purge_deleted_requests(
            bot, group_id=target_group_id, dry_run=True,
            actor_id=message.from_user.id,
            actor_name=message.from_user.username or str(message.from_user.id),
            db=db,
        )

    # Same rule as the per-group settings screen: no confirm button (and no
    # pending record) when the dry run found nothing to act on.
    can_apply = bool(report.deleted_found or report.already_member)
    reply_markup = None
    if can_apply:
        _store_pending_purge(message.from_user.id, target_group_id)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🧹 בצע הסרה", callback_data="purge:apply"))
        reply_markup = builder.as_markup()
        footer = "בדיקה בלבד, עדיין לא הוסר דבר. לחץ על <b>בצע הסרה</b> כדי לבצע."
    else:
        # A stale pending record from an earlier dry run must not survive a
        # newer check that found nothing.
        _PENDING_PURGES.pop(message.from_user.id, None)
        footer = "אין מה להסיר."

    await message.reply(
        f"🧹 <b>בדיקת בקשות ממתינות של חשבונות מחוקים</b>\n\n"
        f"{format_purge_report(report, applied=False)}\n\n{footer}",
        parse_mode="HTML",
        reply_markup=reply_markup,
    )


@router.callback_query(F.data == "purge:apply")
async def cb_purge_apply(callback: CallbackQuery, bot: Bot) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer("⛔ אין הרשאה.", show_alert=True)
        return

    pending = _pop_pending_purge(callback.from_user.id)
    if pending is None:
        await callback.answer("הבדיקה פגה או שכבר בוצעה. יש להריץ /purge_deleted מחדש.", show_alert=True)
        return

    async with AsyncSessionLocal() as db:
        report = await purge_deleted_requests(
            bot, group_id=pending["group_id"], dry_run=False,
            actor_id=callback.from_user.id,
            actor_name=callback.from_user.username or str(callback.from_user.id),
            db=db,
        )

    await callback.answer("בוצע.")
    try:
        await callback.message.edit_text(
            f"🧹 <b>הסרה בוצעה</b>\n\n{format_purge_report(report, applied=True)}",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass
