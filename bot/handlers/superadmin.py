"""Super-admin panel — only accessible by SUPER_ADMIN_ID."""
from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, func

from database.session import AsyncSessionLocal
from database.models import Group, JoinRequest, DecisionEnum, ModerationLog
from config import settings
from logs import get_logger

logger = get_logger(__name__)
router = Router()


def _is_super_admin(user_id: int) -> bool:
    return user_id == settings.super_admin_id


def _groups_kb(groups: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in groups:
        status = "🟢" if g.is_active and not g.is_banned else ("🔴" if g.is_banned else "⏸")
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {g.title or g.chat_id}",
                callback_data=f"sa:group_info:{g.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data="sa:main"))
    return builder.as_markup()


def _group_actions_kb(group_id: int, is_active: bool, is_banned: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if is_banned:
        builder.row(InlineKeyboardButton(text="✅ הסר חסימה", callback_data=f"sa:unban:{group_id}"))
    else:
        builder.row(InlineKeyboardButton(text="🔴 חסום קבוצה", callback_data=f"sa:ban:{group_id}"))
    if is_active:
        builder.row(InlineKeyboardButton(text="⏸ השהה קבוצה", callback_data=f"sa:deactivate:{group_id}"))
    else:
        builder.row(InlineKeyboardButton(text="▶️ הפעל קבוצה", callback_data=f"sa:activate:{group_id}"))
    builder.row(InlineKeyboardButton(text="◀️ חזרה לרשימה", callback_data="sa:groups"))
    return builder.as_markup()


# ─── /admin ───────────────────────────────────────────────────────────────────

@router.message(Command("admin"))
async def cmd_admin(message: Message) -> None:
    if not _is_super_admin(message.from_user.id):
        return

    async with AsyncSessionLocal() as db:
        total_groups = await db.scalar(select(func.count(Group.id)))
        active_groups = await db.scalar(
            select(func.count(Group.id)).where(Group.is_active, ~Group.is_banned)
        )
        total_requests = await db.scalar(select(func.count(JoinRequest.id)))
        approved = await db.scalar(
            select(func.count(JoinRequest.id)).where(JoinRequest.decision == DecisionEnum.approved)
        )

    rate = f"{approved / total_requests * 100:.1f}%" if total_requests else "—"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 כל הקבוצות", callback_data="sa:groups"),
        InlineKeyboardButton(text="📜 לוג אחרון", callback_data="sa:recent_log"),
    )
    builder.row(
        InlineKeyboardButton(text="🔍 חיפוש משתמש", callback_data="sa:search_user"),
        InlineKeyboardButton(text="📢 שידור", callback_data="sa:broadcast"),
    )

    await message.answer(
        f"🔑 <b>פאנל סופר-אדמין</b>\n\n"
        f"📊 קבוצות: {active_groups}/{total_groups} פעילות\n"
        f"📨 סה\"כ בקשות: {total_requests}\n"
        f"✅ אחוז אישור גלובלי: {rate}",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "sa:main")
async def cb_admin_main(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return

    async with AsyncSessionLocal() as db:
        total_groups = await db.scalar(select(func.count(Group.id)))
        active_groups = await db.scalar(
            select(func.count(Group.id)).where(Group.is_active, ~Group.is_banned)
        )
        total_requests = await db.scalar(select(func.count(JoinRequest.id)))
        approved = await db.scalar(
            select(func.count(JoinRequest.id)).where(JoinRequest.decision == DecisionEnum.approved)
        )

    rate = f"{approved / total_requests * 100:.1f}%" if total_requests else "—"

    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 כל הקבוצות", callback_data="sa:groups"),
        InlineKeyboardButton(text="📜 לוג אחרון", callback_data="sa:recent_log"),
    )
    builder.row(
        InlineKeyboardButton(text="🔍 חיפוש משתמש", callback_data="sa:search_user"),
        InlineKeyboardButton(text="📢 שידור", callback_data="sa:broadcast"),
    )

    await callback.message.edit_text(
        f"🔑 <b>פאנל סופר-אדמין</b>\n\n"
        f"📊 קבוצות: {active_groups}/{total_groups} פעילות\n"
        f"📨 סה\"כ בקשות: {total_requests}\n"
        f"✅ אחוז אישור גלובלי: {rate}",
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ─── Groups list ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "sa:groups")
async def cb_groups_list(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Group).order_by(Group.created_at.desc()))
        groups = result.scalars().all()

    await callback.message.edit_text(
        f"📋 <b>כל הקבוצות ({len(groups)})</b>",
        parse_mode="HTML",
        reply_markup=_groups_kb(groups),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sa:group_info:"))
async def cb_group_info(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return

    group_id = int(callback.data.split(":")[2])

    async with AsyncSessionLocal() as db:
        group = await db.get(Group, group_id)
        if not group:
            await callback.answer("קבוצה לא נמצאה.")
            return

        total = await db.scalar(
            select(func.count(JoinRequest.id)).where(JoinRequest.group_id == group_id)
        )
        approved = await db.scalar(
            select(func.count(JoinRequest.id)).where(
                JoinRequest.group_id == group_id,
                JoinRequest.decision == DecisionEnum.approved,
            )
        )

    rate = f"{approved / total * 100:.1f}%" if total else "—"
    status = "🟢 פעילה" if group.is_active and not group.is_banned else ("🔴 חסומה" if group.is_banned else "⏸ מושהית")

    await callback.message.edit_text(
        f"📋 <b>{group.title or 'ללא שם'}</b>\n\n"
        f"🆔 Chat ID: <code>{group.chat_id}</code>\n"
        f"👤 מנהל: <code>{group.owner_id}</code>\n"
        f"📊 בקשות: {total} | ✅ {rate}\n"
        f"סטטוס: {status}\n"
        f"📅 נרשמה: {group.created_at.strftime('%d/%m/%Y') if group.created_at else '—'}",
        parse_mode="HTML",
        reply_markup=_group_actions_kb(group_id, group.is_active, group.is_banned),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sa:ban:"))
async def cb_ban_group(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        group = await db.get(Group, group_id)
        if group:
            group.is_banned = True
            group.is_active = False
            await db.commit()
    await callback.answer("קבוצה נחסמה.", show_alert=True)
    await cb_group_info(callback)


@router.callback_query(F.data.startswith("sa:unban:"))
async def cb_unban_group(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        group = await db.get(Group, group_id)
        if group:
            group.is_banned = False
            group.is_active = True
            await db.commit()
    await callback.answer("חסימה הוסרה.", show_alert=True)
    await cb_group_info(callback)


@router.callback_query(F.data.startswith("sa:deactivate:"))
async def cb_deactivate_group(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        group = await db.get(Group, group_id)
        if group:
            group.is_active = False
            await db.commit()
    await callback.answer("קבוצה הושהתה.", show_alert=True)
    await cb_group_info(callback)


@router.callback_query(F.data.startswith("sa:activate:"))
async def cb_activate_group(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        group = await db.get(Group, group_id)
        if group:
            group.is_active = True
            await db.commit()
    await callback.answer("קבוצה הופעלה.", show_alert=True)
    await cb_group_info(callback)


# ─── Recent global log ────────────────────────────────────────────────────────

@router.callback_query(F.data == "sa:recent_log")
async def cb_recent_log(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ModerationLog).order_by(ModerationLog.created_at.desc()).limit(20)
        )
        logs = result.scalars().all()

    if not logs:
        await callback.answer("אין רשומות עדיין.")
        return

    lines = ["📜 <b>50 רשומות אחרונות</b>\n"]
    for log in logs:
        name = f"{log.first_name or ''} {log.last_name or ''}".strip() or "—"
        icon = {"approved": "✅", "rejected": "❌", "banned": "🔨", "manual_review": "👀", "pending": "⏳"}.get(
            log.decision.value, "?"
        )
        lines.append(f"{icon} {name} | {log.score:+.0f} | gid:{log.group_id or '?'}")

    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data="sa:main"))

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=builder.as_markup(),
    )
    await callback.answer()


# ─── Search user ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "sa:search_user")
async def cb_search_user_prompt(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "🔍 <b>חיפוש משתמש</b>\n\nשלח את ה-Telegram ID של המשתמש:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("search_user"))
async def cmd_search_user(message: Message) -> None:
    if not _is_super_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("שימוש: /search_user <telegram_id>")
        return

    try:
        target_id = int(parts[1])
    except ValueError:
        await message.reply("❌ הכנס ID מספרי.")
        return

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(ModerationLog)
            .where(ModerationLog.user_id == target_id)
            .order_by(ModerationLog.created_at.desc())
            .limit(10)
        )
        logs = result.scalars().all()

    if not logs:
        await message.reply(f"לא נמצאו רשומות עבור {target_id}.")
        return

    lines = [f"🔍 <b>משתמש {target_id}</b>\n"]
    for log in logs:
        icon = {"approved": "✅", "rejected": "❌", "banned": "🔨", "manual_review": "👀", "pending": "⏳"}.get(
            log.decision.value, "?"
        )
        ts = log.created_at.strftime("%d/%m %H:%M") if log.created_at else "—"
        lines.append(f"{icon} gid:{log.group_id or '?'} | {log.score:+.0f} | {ts}")

    await message.reply("\n".join(lines), parse_mode="HTML")


# ─── Broadcast (stub — sends to each group owner) ─────────────────────────────

@router.callback_query(F.data == "sa:broadcast")
async def cb_broadcast_prompt(callback: CallbackQuery) -> None:
    if not _is_super_admin(callback.from_user.id):
        await callback.answer()
        return
    await callback.message.edit_text(
        "📢 <b>שידור הודעה</b>\n\nשלח: /broadcast <טקסט ההודעה>\nההודעה תשלח לכל מנהלי הקבוצות.",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot) -> None:
    if not _is_super_admin(message.from_user.id):
        return

    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        await message.reply("שימוש: /broadcast <הודעה>")
        return

    text = parts[1]

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Group).where(Group.is_active, ~Group.is_banned)
        )
        groups = result.scalars().all()

    sent = 0
    for group in groups:
        try:
            await bot.send_message(
                group.owner_id,
                f"📢 <b>הודעה מהמערכת:</b>\n\n{text}",
                parse_mode="HTML",
            )
            sent += 1
        except TelegramAPIError:
            pass

    await message.reply(f"✅ הודעה נשלחה ל-{sent}/{len(groups)} מנהלים.")
