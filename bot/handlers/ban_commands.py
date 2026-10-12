"""Manual user-ban commands: /ban, /unban, /bans (access path 1).

The actual ban/unban logic lives in moderation/actions.py and is shared with
the manual-review button, the super-admin panel, and the terminal script.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Optional

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup
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


# A purge sweep probes every open row and can outlive a callback query by far,
# so it never runs inline in a callback handler: the handler answers at once
# and the sweep runs as a task that edits the message as it goes. Tasks are
# kept referenced here so they are not garbage collected mid-run.
_PURGE_TASKS: set[asyncio.Task] = set()
_PURGE_RUNNING: set[int] = set()   # admin ids with a sweep in flight
_PURGE_PROGRESS_INTERVAL = 3.0     # seconds between progress edits

PurgeSweep = Callable[[Callable[[int, int], Awaitable[None]]], Awaitable[tuple[str, Optional[InlineKeyboardMarkup]]]]


async def safe_answer(callback: CallbackQuery, text: Optional[str] = None, show_alert: bool = False) -> None:
    """Answers a callback query without ever raising: an expired query must not
    kill the handler before it gets to show the admin anything."""
    try:
        if text is None:
            await callback.answer()
        elif show_alert:
            await callback.answer(text, show_alert=True)
        else:
            await callback.answer(text)
    except TelegramAPIError as e:
        logger.warning(f"callback answer failed ({callback.data}): {e}")


async def _safe_edit(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> bool:
    """Never raises: besides TelegramAPIError (deleted message, 429, ...), the
    message handed in may be an InaccessibleMessage (no edit_text method at
    all) or even None (callback.message can genuinely be None), which would
    otherwise surface as an AttributeError and escape the background task."""
    try:
        await message.edit_text(text, parse_mode="HTML", reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.warning(f"purge message edit failed: {e}")
        return False


async def _safe_edit_or_send(message: Message, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> bool:
    """Edits the message in place; if that fails (deleted message, inaccessible
    message, network error, a 429 on the edit), sends a new message instead so
    the admin is never left without feedback. Returns whether the admin was
    reached either way, so the caller can fall back further if not."""
    if await _safe_edit(message, text, reply_markup=reply_markup):
        return True
    try:
        await message.answer(text, parse_mode="HTML", reply_markup=reply_markup)
        return True
    except Exception as e:
        logger.error(f"purge fallback message send failed: {e}")
        return False


def purge_in_progress(admin_id: int) -> bool:
    return admin_id in _PURGE_RUNNING


def start_purge_task(
    message: Message,
    admin_id: int,
    header: str,
    sweep: PurgeSweep,
    bot: Bot,
    failure_markup: Optional[InlineKeyboardMarkup] = None,
) -> asyncio.Task:
    """Runs `sweep` in the background. The message always ends with either the
    sweep's report or an explicit failure line, never with silence - including
    when the original message itself cannot be reached at all."""
    _PURGE_RUNNING.add(admin_id)

    async def _tell_admin(text: str, reply_markup: Optional[InlineKeyboardMarkup] = None) -> None:
        """Last resort: if the original message is gone, inaccessible, or was
        None to begin with, send the admin a fresh private message directly
        instead of leaving them with no answer at all."""
        if message is not None and await _safe_edit_or_send(message, text, reply_markup=reply_markup):
            return
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception:
            logger.exception("purge: could not reach admin at all")

    async def runner() -> None:
        try:
            await _safe_edit(message, f"{header}\n\n⏳ הבדיקה התחילה. ההודעה תתעדכן בסיום.")

            async def progress(done: int, total: int) -> None:
                await _safe_edit(message, f"{header}\n\n⏳ נבדקו {done} מתוך {total} בקשות...")

            try:
                text, markup = await sweep(progress)
            except Exception:
                logger.exception("purge sweep failed")
                await _tell_admin(
                    f"{header}\n\n❌ הבדיקה נכשלה באמצע. ייתכן שחלק מהשורות כבר טופלו. "
                    f"אפשר להריץ אותה שוב, היא בטוחה להרצה חוזרת.",
                    reply_markup=failure_markup,
                )
                return

            await _tell_admin(text, reply_markup=markup)
        except Exception:
            # Catches anything _safe_edit/_tell_admin themselves did not
            # already absorb, so the task can never die silently.
            logger.exception("purge: background task failed outside the sweep")
            await _tell_admin(
                f"{header}\n\n❌ הבדיקה נכשלה באופן בלתי צפוי. אפשר להריץ אותה שוב.",
                reply_markup=failure_markup,
            )
        finally:
            _PURGE_RUNNING.discard(admin_id)

    task = asyncio.create_task(runner())
    _PURGE_TASKS.add(task)
    task.add_done_callback(_PURGE_TASKS.discard)
    return task


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

        # A message handler has no answer deadline, but a failed sweep must
        # still end with a reply rather than a dead update.
        try:
            report = await purge_deleted_requests(
                bot, group_id=target_group_id, dry_run=True,
                actor_id=message.from_user.id,
                actor_name=message.from_user.username or str(message.from_user.id),
                db=db,
            )
        except Exception:
            logger.exception("purge_deleted dry run failed")
            _PENDING_PURGES.pop(message.from_user.id, None)
            await message.reply("❌ הבדיקה נכשלה באמצע. אפשר להריץ /purge_deleted שוב.")
            return

    # Same rule as the per-group settings screen: no confirm button (and no
    # pending record) when the dry run found nothing to act on.
    can_apply = bool(report.deleted_found or report.already_member)
    reply_markup = None
    if can_apply:
        _store_pending_purge(message.from_user.id, target_group_id)
        # The scope travels with the button (group id, or "all" for a global
        # sweep) so cb_purge_apply can refuse a button whose scope does not
        # match whatever dry run is currently pending for this admin.
        scope_token = "all" if target_group_id is None else str(target_group_id)
        builder = InlineKeyboardBuilder()
        builder.row(InlineKeyboardButton(text="🧹 בצע הסרה", callback_data=f"purge:apply:{scope_token}"))
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


@router.callback_query(F.data.startswith("purge:apply"))
async def cb_purge_apply(callback: CallbackQuery, bot: Bot) -> None:
    if not _is_super_admin(callback.from_user.id):
        await safe_answer(callback, "⛔ אין הרשאה.", show_alert=True)
        return

    if purge_in_progress(callback.from_user.id):
        await safe_answer(callback, "בדיקה אחרת עדיין רצה. ההודעה שלה תתעדכן בסיום.", show_alert=True)
        return

    # The scope travels with the button: a bare legacy "purge:apply" (an old
    # message from before this check existed) carries no scope at all and is
    # refused outright rather than trusted against whatever happens to be
    # pending for this admin right now.
    parts = callback.data.split(":")
    if len(parts) != 3:
        await safe_answer(callback, "הכפתור הזה ישן. יש להריץ /purge_deleted מחדש.", show_alert=True)
        return
    scope_token = parts[2]
    if scope_token == "all":
        button_group_id = None
    else:
        try:
            button_group_id = int(scope_token)
        except ValueError:
            await safe_answer(callback, "כפתור לא תקין.", show_alert=True)
            return

    # One-shot: the record is consumed here regardless of outcome, so a stale
    # or mismatched button can never be retried against a newer dry run.
    pending = _pop_pending_purge(callback.from_user.id)
    if pending is None:
        await safe_answer(callback, "הבדיקה פגה או שכבר בוצעה. יש להריץ /purge_deleted מחדש.", show_alert=True)
        return
    if pending["group_id"] != button_group_id:
        await safe_answer(
            callback,
            "הכפתור הזה שייך לבדיקה אחרת מזו שמוצגת כרגע. יש להריץ בדיקה חדשה וללחוץ על הכפתור שלה.",
            show_alert=True,
        )
        return

    # Answer before any probing: the query expires long before a sweep ends.
    await safe_answer(callback, "ההסרה התחילה. ההודעה תתעדכן בסיום.")

    actor_id = callback.from_user.id
    actor_name = callback.from_user.username or str(callback.from_user.id)

    async def sweep(progress):
        async with AsyncSessionLocal() as db:
            report = await purge_deleted_requests(
                bot, group_id=pending["group_id"], dry_run=False,
                actor_id=actor_id, actor_name=actor_name, db=db,
                progress=progress, progress_interval=_PURGE_PROGRESS_INTERVAL,
            )
        return f"🧹 <b>הסרה בוצעה</b>\n\n{format_purge_report(report, applied=True)}", None

    start_purge_task(callback.message, actor_id, "🧹 <b>הסרת בקשות של חשבונות מחוקים</b>", sweep, bot)
