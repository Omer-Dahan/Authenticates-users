"""FSM-based blacklist and whitelist management through the bot."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import GroupBlacklist, GroupWhitelist
from bot.keyboards.settings_kb import (
    blacklist_list_kb, whitelist_list_kb, back_to_main_kb,
)
from logs import get_logger

logger = get_logger(__name__)
router = Router()


class AddBlacklistState(StatesGroup):
    entering_keyword = State()
    entering_score = State()


class AddWhitelistState(StatesGroup):
    entering_user_id = State()
    entering_notes = State()


# ─── Blacklist ────────────────────────────────────────────────────────────────

async def _show_blacklist(callback: CallbackQuery, group_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupBlacklist).where(GroupBlacklist.group_id == group_id).order_by(GroupBlacklist.created_at)
        )
        entries = result.scalars().all()
    await callback.message.edit_text(
        f"🚫 <b>רשימה שחורה ({len(entries)} מילים)</b>\n"
        f"מילים ספציפיות המורידות ניקוד אם הן מופיעות בשמו או בפרטיו של המצטרף החדש.\n\n"
        f"בחר מילה למחיקה או הוסף מילה חדשה:",
        parse_mode="HTML",
        reply_markup=blacklist_list_kb(group_id, entries),
    )


@router.callback_query(F.data.startswith("sm:blacklist:"))
async def cb_blacklist_menu(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    await _show_blacklist(callback, group_id)
    await callback.answer()


@router.callback_query(F.data.startswith("bl:delete:"))
async def cb_bl_delete(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    entry_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        entry = await db.get(GroupBlacklist, entry_id)
        if entry and entry.group_id == group_id:
            await db.delete(entry)
            await db.commit()
            await callback.answer("נמחק.")
        else:
            await callback.answer("לא נמצא.")

    await _show_blacklist(callback, group_id)


@router.callback_query(F.data.startswith("bl:add:"))
async def cb_bl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[2])
    await state.set_state(AddBlacklistState.entering_keyword)
    await state.update_data(group_id=group_id)
    await callback.message.edit_text(
        "🚫 <b>הוספת מילה לרשימה שחורה</b>\n\nהזן מילת מפתח:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AddBlacklistState.entering_keyword, F.chat.type == "private")
async def fsm_bl_enter_keyword(message: Message, state: FSMContext) -> None:
    await state.update_data(keyword=message.text.strip().lower())
    await state.set_state(AddBlacklistState.entering_score)
    await message.answer("הזן ניקוד (ברירת מחדל: -100):")


@router.message(AddBlacklistState.entering_score, F.chat.type == "private")
async def fsm_bl_enter_score(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    if text == "":
        score = -100.0
    else:
        try:
            score = float(text)
        except ValueError:
            await message.answer("❌ הכנס מספר. נסה שנית:")
            return

    data = await state.get_data()
    await state.clear()
    group_id = data["group_id"]
    keyword = data["keyword"]

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(GroupBlacklist).where(
                GroupBlacklist.group_id == group_id,
                GroupBlacklist.keyword == keyword,
            )
        )
        if existing.scalar_one_or_none():
            await message.answer(f"⚠️ המילה '{keyword}' כבר קיימת ברשימה.", reply_markup=back_to_main_kb(group_id))
            return
        db.add(GroupBlacklist(group_id=group_id, keyword=keyword, score=score))
        await db.commit()

    await message.answer(
        f"✅ <b>'{keyword}'</b> נוסף לרשימה שחורה (ניקוד: {score:+.0f})",
        parse_mode="HTML",
        reply_markup=back_to_main_kb(group_id),
    )


@router.callback_query(F.data.startswith("bl:noop:"))
async def cb_bl_noop(callback: CallbackQuery) -> None:
    await callback.answer()


# ─── Whitelist ────────────────────────────────────────────────────────────────

async def _show_whitelist(callback: CallbackQuery, group_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupWhitelist).where(GroupWhitelist.group_id == group_id).order_by(GroupWhitelist.created_at)
        )
        entries = result.scalars().all()
    await callback.message.edit_text(
        f"✅ <b>רשימה לבנה ({len(entries)} משתמשים)</b>\n"
        f"משתמשים לפי Telegram ID שיאושרו באופן מיידי ואוטומטי ללא צורך במעבר סינון.\n\n"
        f"בחר משתמש למחיקה או הוסף משתמש חדש:",
        parse_mode="HTML",
        reply_markup=whitelist_list_kb(group_id, entries),
    )


@router.callback_query(F.data.startswith("sm:whitelist:"))
async def cb_whitelist_menu(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    await _show_whitelist(callback, group_id)
    await callback.answer()


@router.callback_query(F.data.startswith("wl:delete:"))
async def cb_wl_delete(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    entry_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        entry = await db.get(GroupWhitelist, entry_id)
        if entry and entry.group_id == group_id:
            await db.delete(entry)
            await db.commit()
            await callback.answer("נמחק.")
        else:
            await callback.answer("לא נמצא.")

    await _show_whitelist(callback, group_id)


@router.callback_query(F.data.startswith("wl:add:"))
async def cb_wl_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[2])
    await state.set_state(AddWhitelistState.entering_user_id)
    await state.update_data(group_id=group_id)
    await callback.message.edit_text(
        "✅ <b>הוספת משתמש לרשימה לבנה</b>\n\nהזן את ה-Telegram ID של המשתמש:",
        parse_mode="HTML",
    )
    await callback.answer()


@router.message(AddWhitelistState.entering_user_id, F.chat.type == "private")
async def fsm_wl_enter_user_id(message: Message, state: FSMContext) -> None:
    try:
        telegram_id = int(message.text.strip())
    except ValueError:
        await message.answer("❌ הכנס מספר ID. נסה שנית:")
        return
    await state.update_data(telegram_id=telegram_id)
    await state.set_state(AddWhitelistState.entering_notes)
    await message.answer("הזן הערה (אופציונלי — שלח - לדלג):")


@router.message(AddWhitelistState.entering_notes, F.chat.type == "private")
async def fsm_wl_enter_notes(message: Message, state: FSMContext) -> None:
    notes = None if message.text.strip() == "-" else message.text.strip()
    data = await state.get_data()
    await state.clear()

    group_id = data["group_id"]
    telegram_id = data["telegram_id"]

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(GroupWhitelist).where(
                GroupWhitelist.group_id == group_id,
                GroupWhitelist.telegram_id == telegram_id,
            )
        )
        if existing.scalar_one_or_none():
            await message.answer(f"⚠️ המשתמש {telegram_id} כבר ברשימה.", reply_markup=back_to_main_kb(group_id))
            return
        db.add(GroupWhitelist(group_id=group_id, telegram_id=telegram_id, notes=notes))
        await db.commit()

    await message.answer(
        f"✅ <b>משתמש {telegram_id} נוסף לרשימה הלבנה</b>",
        parse_mode="HTML",
        reply_markup=back_to_main_kb(group_id),
    )


@router.callback_query(F.data.startswith("wl:noop:"))
async def cb_wl_noop(callback: CallbackQuery) -> None:
    await callback.answer()
