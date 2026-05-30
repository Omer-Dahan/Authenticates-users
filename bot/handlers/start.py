"""Private-chat /start command — bot info + group management dashboard."""
from aiogram import Router, Bot, F
from aiogram.filters import CommandStart
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import Group
from bot.keyboards.settings_kb import main_settings_kb
from logs import get_logger

logger = get_logger(__name__)
router = Router()


def _groups_kb(groups: list[Group], bot_username: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in groups:
        status = "🟢" if g.is_active else "🔴"
        title = (g.title or str(g.chat_id))[:30]
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {title}",
                callback_data=f"start:group:{g.id}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="➕ הוסף את הבוט לקבוצה",
            url=f"https://t.me/{bot_username}?startgroup=setup",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="📖 מדריך למשתמש ושאלות נפוצות",
            url="https://telegra.ph/מדריך-למשתמש-ושאלות-נפוצות---בוט-סינון-הצטרפות-05-30-2",
        )
    )
    builder.row(
        InlineKeyboardButton(text="🔄 רענן", callback_data="start:refresh"),
    )
    return builder.as_markup()



async def _send_dashboard(message: Message, bot: Bot, edit: bool = False) -> None:
    user_id = message.chat.id if edit else message.from_user.id

    me = await bot.get_me()
    bot_username = me.username

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Group).where(Group.owner_id == user_id, Group.is_banned == False)
        )
        groups = result.scalars().all()

    logger.info("Dashboard loaded", user_id=user_id, group_count=len(groups), edit=edit)

    if groups:
        groups_text = "\n".join(
            f"  {'🟢' if g.is_active else '🔴'} {g.title or g.chat_id}"
            for g in groups
        )
        body = (
            f"<b>הקבוצות שלך ({len(groups)}):</b>\n"
            f"{groups_text}\n\n"
            "לחץ על קבוצה לניהולה:"
        )
    else:
        body = (
            "עדיין לא רשמת קבוצות.\n\n"
            "לחץ על <b>הוסף את הבוט לקבוצה</b> כדי להתחיל:"
        )

    text = (
        "🛡️ <b>בוט מודרציה לבקשות הצטרפות</b>\n\n"
        "הבוט שולט על מי מצטרף לקבוצות שלך — "
        "הוא בודק כל בקשת הצטרפות לפי כללים שתגדיר "
        "(שמות, שפות, ביטויים חשודים) ומאשר, דוחה, או מעביר לסקירה ידנית.\n\n"
        "✅ אישור / דחייה אוטומטיים לפי ניקוד\n"
        "🔍 שאלות אימות\n"
        "📋 רשימות שחור ולבן\n"
        "🛡️ מצבי אבטחה: רגיל / קפדני / נעילה\n\n"
        "📖 <a href='https://telegra.ph/מדריך-למשתמש-ושאלות-נפוצות---בוט-סינון-הצטרפות-05-30-2'>מדריך למשתמש ושאלות נפוצות</a>\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"{body}"
    )

    kb = _groups_kb(groups, bot_username)

    if edit:
        try:
            await message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except Exception:
            await message.answer(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)


# ─── /start ───────────────────────────────────────────────────────────────────

@router.message(CommandStart(), F.chat.type == "private")
async def cmd_start(message: Message, bot: Bot) -> None:
    logger.info("/start received", user_id=message.from_user.id, username=message.from_user.username)
    await _send_dashboard(message, bot)


# ─── Refresh dashboard ────────────────────────────────────────────────────────

@router.callback_query(F.data == "start:refresh")
async def cb_refresh(callback: CallbackQuery, bot: Bot) -> None:
    logger.info("Refresh requested", user_id=callback.from_user.id, chat_id=callback.message.chat.id)
    await _send_dashboard(callback.message, bot, edit=True)
    await callback.answer("🔄 רשימה עודכנה")


# ─── Group detail ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("start:group:"))
async def cb_group_detail(callback: CallbackQuery, bot: Bot) -> None:
    group_id = int(callback.data.split(":")[2])

    async with AsyncSessionLocal() as db:
        group = await db.get(Group, group_id)

    if not group or group.owner_id != callback.from_user.id:
        await callback.answer("קבוצה לא נמצאה.", show_alert=True)
        return

    status = "🟢 פעיל" if group.is_active else "🔴 מושהה"
    username_part = f"@{group.username}" if group.username else str(group.chat_id)

    text = (
        f"📋 <b>{group.title or group.chat_id}</b>\n\n"
        f"מזהה: <code>{username_part}</code>\n"
        f"סטטוס: {status}\n\n"
        "בחר פעולה:"
    )
    await callback.message.edit_text(
        text, parse_mode="HTML", reply_markup=main_settings_kb(group_id)
    )
    await callback.answer()
