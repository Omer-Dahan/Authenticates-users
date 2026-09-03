"""Main settings menu, security mode, thresholds, stats, and manual review callbacks."""
from datetime import datetime, timezone

from aiogram import Router, Bot, F
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from sqlalchemy import select, func

from database.session import AsyncSessionLocal
from database.models import (
    Group, GroupConfig, GroupLanguageFilter,
    JoinRequest, DecisionEnum, ModerationLog,
)
from database.clone_utils import get_difference_summary, clone_group_settings
from bot.keyboards.settings_kb import (
    main_settings_kb, security_mode_kb, back_to_main_kb, languages_list_kb,
    import_list_kb, import_confirm_kb, notify_settings_kb,
)
from bot.handlers.admin_utils import register_group_admin, mark_settings_edited
from logs import get_logger

logger = get_logger(__name__)
router = Router()


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except TelegramAPIError:
        return False


async def _get_group(chat_id: int, db) -> Group | None:
    result = await db.execute(select(Group).where(Group.chat_id == chat_id))
    return result.scalar_one_or_none()


async def _get_group_by_id(group_id: int, db) -> Group | None:
    return await db.get(Group, group_id)


async def _set_config(group_id: int, key: str, value: str, value_type: str, db) -> None:
    result = await db.execute(
        select(GroupConfig).where(GroupConfig.group_id == group_id, GroupConfig.key == key)
    )
    cfg = result.scalar_one_or_none()
    if cfg:
        cfg.value = value
        cfg.value_type = value_type
    else:
        db.add(GroupConfig(group_id=group_id, key=key, value=value, value_type=value_type))


# ─── /settings ────────────────────────────────────────────────────────────────

@router.message(Command("settings"))
async def cmd_settings(message: Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("השתמש ב-/settings בתוך קבוצה רשומה.")
        return

    user_id = message.from_user.id
    chat_id = message.chat.id

    if not await _is_chat_admin(bot, chat_id, user_id):
        await message.reply("❌ רק מנהלי הקבוצה יכולים לגשת להגדרות.")
        return

    async with AsyncSessionLocal() as db:
        group = await _get_group(chat_id, db)
        if group:
            await register_group_admin(group.id, user_id, message.from_user.username, message.from_user.first_name, db)
            await db.commit()

    if not group:
        await message.reply("❌ הקבוצה אינה רשומה. הפעל /setup תחילה.")
        return

    try:
        await bot.send_message(
            user_id,
            f"⚙️ <b>הגדרות קבוצה: {group.title}</b>\n\nבחר אפשרות:",
            parse_mode="HTML",
            reply_markup=main_settings_kb(group.id),
        )
        await message.reply("📬 שלחתי לך הודעה פרטית עם ההגדרות.")
    except TelegramAPIError:
        await message.reply(
            f"⚙️ <b>הגדרות קבוצה: {group.title}</b>\n\nבחר אפשרות:",
            parse_mode="HTML",
            reply_markup=main_settings_kb(group.id),
        )


# ─── Main menu navigation ─────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:main:"))
async def cb_main_menu(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        group = await _get_group_by_id(group_id, db)
    if not group:
        await callback.answer("קבוצה לא נמצאה.")
        return
    await callback.message.edit_text(
        f"⚙️ <b>הגדרות קבוצה: {group.title}</b>\n\nבחר אפשרות:",
        parse_mode="HTML",
        reply_markup=main_settings_kb(group_id),
    )
    await callback.answer()


# ─── Security mode ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:mode:"))
async def cb_mode_menu(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupConfig).where(GroupConfig.group_id == group_id, GroupConfig.key == "security_mode")
        )
        cfg = result.scalar_one_or_none()
        current = cfg.value if cfg else "normal"

    icons = {"normal": "🟢 רגיל", "strict": "🟡 קפדני", "lockdown": "🔴 נעילה"}
    await callback.message.edit_text(
        f"🛡️ <b>מצב אבטחה נוכחי:</b> {icons.get(current, current)}\n\n"
        f"מצב האבטחה קובע כיצד הבוט מסנן משתמשים חדשים:\n"
        f"• <b>רגיל</b>: סינון אוטומטי לפי ניקוד שנצבר מכללים, שפות ושאלות.\n"
        f"• <b>קפדני</b>: מחייב את כל המשתמשים החדשים לענות על שאלות אימות.\n"
        f"• <b>נעילה</b>: מעביר את כל המשתמשים החדשים ישירות לסקירה ידנית של מנהלים.\n\n"
        f"בחר מצב חדש:",
        parse_mode="HTML",
        reply_markup=security_mode_kb(group_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sm:set_mode:"))
async def cb_set_mode(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    mode = parts[3]

    if mode not in ("normal", "strict", "lockdown"):
        await callback.answer("מצב לא חוקי.")
        return

    async with AsyncSessionLocal() as db:
        await _set_config(group_id, "security_mode", mode, "string", db)
        await mark_settings_edited(group_id, callback.from_user.id, callback.from_user.username, callback.from_user.first_name, db)
        await db.commit()

    icons = {"normal": "🟢 רגיל", "strict": "🟡 קפדני", "lockdown": "🔴 נעילה"}
    await callback.answer(f"מצב אבטחה שונה ל: {icons[mode]}", show_alert=True)
    await callback.message.edit_text(
        f"🛡️ <b>מצב אבטחה שונה ל:</b> {icons[mode]}",
        parse_mode="HTML",
        reply_markup=back_to_main_kb(group_id),
    )


# ─── Manual-review admin notifications ─────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:notify:"))
async def cb_notify_menu(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupConfig).where(
                GroupConfig.group_id == group_id, GroupConfig.key == "notify_admin_on_manual_review"
            )
        )
        cfg = result.scalar_one_or_none()
        enabled = cfg.value.lower() == "true" if cfg else False

    status = "🔔 פעיל" if enabled else "🔕 כבוי"
    await callback.message.edit_text(
        f"🔔 <b>התראות סקירה ידנית</b>\n\n"
        f"מצב נוכחי: {status}\n\n"
        f"כאשר מופעל, הבוט ישלח לך הודעה פרטית עם כפתורי אישור/דחייה/חסימה "
        f"עבור כל בקשת הצטרפות שנופלת בטווח הסקירה הידנית.\n"
        f"כברירת מחדל ההתראות כבויות — ניתן לעקוב אחרי הבקשות דרך /settings ← סטטיסטיקות.",
        parse_mode="HTML",
        reply_markup=notify_settings_kb(group_id, enabled),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sm:set_notify:"))
async def cb_set_notify(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    state = parts[3]

    if state not in ("on", "off"):
        await callback.answer("ערך לא חוקי.")
        return

    enabled = state == "on"
    async with AsyncSessionLocal() as db:
        await _set_config(group_id, "notify_admin_on_manual_review", "true" if enabled else "false", "bool", db)
        await mark_settings_edited(group_id, callback.from_user.id, callback.from_user.username, callback.from_user.first_name, db)
        await db.commit()

    status = "🔔 פעיל" if enabled else "🔕 כבוי"
    await callback.answer(f"התראות סקירה ידנית: {status}", show_alert=True)
    await callback.message.edit_text(
        f"🔔 <b>התראות סקירה ידנית שונו ל:</b> {status}",
        parse_mode="HTML",
        reply_markup=back_to_main_kb(group_id),
    )


# ─── Statistics ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:stats:"))
async def cb_stats(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])

    async with AsyncSessionLocal() as db:
        group = await _get_group_by_id(group_id, db)

        counts = {}
        for decision in DecisionEnum:
            count = await db.scalar(
                select(func.count(JoinRequest.id)).where(
                    JoinRequest.group_id == group_id,
                    JoinRequest.decision == decision,
                )
            )
            counts[decision] = count or 0

        total = sum(counts.values())
        approved = counts[DecisionEnum.approved]
        rate = f"{approved / total * 100:.1f}%" if total else "—"

    text = (
        f"📊 <b>סטטיסטיקות: {group.title if group else group_id}</b>\n"
        f"ריכוז נתוני ההצטרפות לקבוצה ופעולות המודרציה שבוצעו:\n\n"
        f"✅ אושרו: {counts[DecisionEnum.approved]}\n"
        f"❌ נדחו: {counts[DecisionEnum.rejected]}\n"
        f"🔨 חסומים: {counts[DecisionEnum.banned]}\n"
        f"👀 סקירה ידנית: {counts[DecisionEnum.manual_review]}\n"
        f"⏳ ממתינים: {counts[DecisionEnum.pending]}\n"
        f"─────────────────\n"
        f"סה\"כ: {total} | אחוז אישור: {rate}"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_to_main_kb(group_id))
    await callback.answer()


# ─── Language filters ─────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:languages:"))
async def cb_languages(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupLanguageFilter).where(GroupLanguageFilter.group_id == group_id)
        )
        filters = result.scalars().all()

    await callback.message.edit_text(
        "🌐 <b>פילטרי שפה</b>\n"
        "זיהוי שפות בשם המשתמש. שפה מופעלת תעניק או תוריד ניקוד בהתאם להגדרה שלה.\n\n"
        "לחץ על שפה להפעלה/כיבוי:",
        parse_mode="HTML",
        reply_markup=languages_list_kb(group_id, filters),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("lang:toggle:"))
async def cb_lang_toggle(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    lang_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        lang = await db.get(GroupLanguageFilter, lang_id)
        if lang and lang.group_id == group_id:
            lang.enabled = not lang.enabled
            await db.commit()
            state = "הופעל" if lang.enabled else "כובה"
            await callback.answer(f"{lang.language} {state}")
        else:
            await callback.answer("שגיאה.")
            return

        result = await db.execute(
            select(GroupLanguageFilter).where(GroupLanguageFilter.group_id == group_id)
        )
        filters = result.scalars().all()

    await callback.message.edit_reply_markup(reply_markup=languages_list_kb(group_id, filters))


# ─── Thresholds ───────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:thresholds:"))
async def cb_thresholds(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])

    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupConfig).where(GroupConfig.group_id == group_id)
        )
        configs = {c.key: c.value for c in result.scalars().all()}

    text = (
        "📊 <b>ספי ניקוד</b>\n"
        "קובעים את הפעולה שתתבצע לפי הניקוד המצטבר של המשתמש:\n\n"
        f"✅ אישור אוטומטי: {configs.get('approve_threshold', '60')}\n"
        f"❌ דחייה אוטומטית: {configs.get('reject_threshold', '0')}\n"
        f"🔨 חסימה אוטומטית: {configs.get('auto_ban_threshold', '-100')}\n"
        f"👀 סקירה ידנית: {configs.get('manual_review_range_min', '30')}–{configs.get('manual_review_range_max', '60')}\n"
        f"🔍 סף התאמת שמות: {configs.get('fuzzy_match_threshold', '80')}%\n\n"
        "<i>לשינוי ספים, שלח: /threshold &lt;מפתח&gt; &lt;ערך&gt;\n"
        "לדוגמה: /threshold approve_threshold 70</i>"
    )
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=back_to_main_kb(group_id))
    await callback.answer()


@router.message(Command("threshold"), F.chat.type == "private")
async def cmd_threshold(message: Message) -> None:
    parts = message.text.split(maxsplit=2)
    if len(parts) != 3:
        await message.reply("שימוש: /threshold <מפתח> <ערך>\nלדוגמה: /threshold approve_threshold 70")
        return

    _, key, value = parts
    valid_keys = {
        "approve_threshold": "float",
        "reject_threshold": "float",
        "auto_ban_threshold": "float",
        "manual_review_range_min": "float",
        "manual_review_range_max": "float",
        "fuzzy_match_threshold": "float",
    }

    if key not in valid_keys:
        await message.reply("מפתח לא חוקי. מפתחות אפשריים:\n" + "\n".join(valid_keys))
        return

    try:
        float(value)
    except ValueError:
        await message.reply("הערך חייב להיות מספר.")
        return

    user_id = message.from_user.id
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Group).where(Group.owner_id == user_id, Group.is_active)
        )
        groups = result.scalars().all()

        if not groups:
            await message.reply("לא נמצאו קבוצות שלך.")
            return

        for group in groups:
            await _set_config(group.id, key, value, valid_keys[key], db)
        await db.commit()

    names = ", ".join(g.title or str(g.chat_id) for g in groups)
    await message.reply(f"✅ {key} עודכן ל-{value} בקבוצות: {names}")


# ─── Manual review callbacks (approve/reject/ban via inline buttons) ──────────

@router.callback_query(F.data.startswith("rev:"))
async def cb_manual_review(callback: CallbackQuery, bot: Bot) -> None:
    parts = callback.data.split(":")
    action = parts[1]
    user_id = int(parts[2])
    join_request_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        join_req = await db.get(JoinRequest, join_request_id)
        if not join_req or join_req.decision not in (DecisionEnum.pending, DecisionEnum.manual_review):
            await callback.answer("הבקשה כבר טופלה.")
            return

        reviewer = callback.from_user.username or str(callback.from_user.id)

        if action == "approve":
            try:
                await bot.approve_chat_join_request(join_req.chat_id, user_id)
            except TelegramAPIError as e:
                await callback.answer(f"שגיאה: {e}")
                return
            join_req.decision = DecisionEnum.approved
            label = "✅ אושר"

        elif action == "reject":
            try:
                await bot.decline_chat_join_request(join_req.chat_id, user_id)
            except TelegramAPIError as e:
                await callback.answer(f"שגיאה: {e}")
                return
            join_req.decision = DecisionEnum.rejected
            label = "❌ נדחה"

        elif action == "ban":
            try:
                await bot.ban_chat_member(join_req.chat_id, user_id)
                await bot.decline_chat_join_request(join_req.chat_id, user_id)
            except TelegramAPIError as e:
                await callback.answer(f"שגיאה: {e}")
                return
            join_req.decision = DecisionEnum.banned
            label = "🔨 חסום"

        else:
            await callback.answer("פעולה לא חוקית.")
            return

        join_req.reviewed_by = reviewer
        join_req.resolved_at = datetime.now(timezone.utc)

        db.add(ModerationLog(
            group_id=join_req.group_id,
            join_request_id=join_req.id,
            user_id=user_id,
            decision=join_req.decision,
            score=join_req.score,
            matched_rules=join_req.matched_rules,
            details={"manual_review": True, "reviewer": reviewer},
        ))
        await db.commit()

    await callback.answer(f"{label} על ידי @{reviewer}")
    try:
        await callback.message.edit_text(
            callback.message.text + f"\n\n{label} על ידי @{reviewer}",
            parse_mode="HTML",
        )
    except TelegramAPIError:
        pass


# ─── Import / Export Settings ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:set_default:"))
async def cb_set_default(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    
    async with AsyncSessionLocal() as db:
        group = await _get_group_by_id(group_id, db)
        if not group:
            await callback.answer("קבוצה לא נמצאה.")
            return
            
        owner_id = group.owner_id
        # Reset is_default_template for all groups of this owner
        owner_groups_result = await db.execute(select(Group).where(Group.owner_id == owner_id))
        for g in owner_groups_result.scalars():
            g.is_default_template = False
            
        # Set this group as default
        group.is_default_template = True
        await mark_settings_edited(group_id, callback.from_user.id, callback.from_user.username, callback.from_user.first_name, db)
        await db.commit()

    await callback.answer("הגדרות קבוצה זו נקבעו כברירת מחדל לקבוצות חדשות שתעלה!", show_alert=True)


@router.callback_query(F.data.startswith("sm:import:"))
async def cb_import(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    
    async with AsyncSessionLocal() as db:
        group = await _get_group_by_id(group_id, db)
        if not group:
            await callback.answer("קבוצה לא נמצאה.")
            return
            
        owner_id = group.owner_id
        # Get all other groups of this owner
        other_groups_result = await db.execute(
            select(Group).where(Group.owner_id == owner_id, Group.id != group_id)
        )
        other_groups = other_groups_result.scalars().all()
        
    if not other_groups:
        await callback.answer("אין לך קבוצות נוספות שניתן לייבא מהן הגדרות.", show_alert=True)
        return
        
    await callback.message.edit_text(
        "🔄 <b>ייבוא הגדרות</b>\n\n"
        "בחר קבוצה ממנה תרצה לייבא את ההגדרות לקבוצה הנוכחית:",
        parse_mode="HTML",
        reply_markup=import_list_kb(group_id, other_groups)
    )


@router.callback_query(F.data.startswith("sm:imp_from:"))
async def cb_imp_from(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    target_group_id = int(parts[2])
    source_group_id = int(parts[3])
    
    async with AsyncSessionLocal() as db:
        source_group = await _get_group_by_id(source_group_id, db)
        if not source_group:
            await callback.answer("קבוצת מקור לא נמצאה.")
            return
            
        diff_summary = await get_difference_summary(source_group_id, target_group_id, db)
        
    await callback.message.edit_text(
        f"⚠️ <b>אזהרת דריסה!</b>\n\n"
        f"אתה עומד לייבא הגדרות מ-<b>{source_group.title}</b> לקבוצה הנוכחית.\n\n"
        f"פעולה זו תמחק את ההגדרות, הכללים והשאלות הנוכחיים ותחליף אותם!\n\n"
        f"{diff_summary}\n\n"
        f"האם אתה בטוח שברצונך להמשיך?",
        parse_mode="HTML",
        reply_markup=import_confirm_kb(target_group_id, source_group_id)
    )


@router.callback_query(F.data.startswith("sm:imp_conf:"))
async def cb_imp_conf(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    target_group_id = int(parts[2])
    source_group_id = int(parts[3])
    
    async with AsyncSessionLocal() as db:
        await clone_group_settings(source_group_id, target_group_id, db)
        await mark_settings_edited(target_group_id, callback.from_user.id, callback.from_user.username, callback.from_user.first_name, db)
        await db.commit()

    await callback.answer("✅ ההגדרות יובאו בהצלחה!", show_alert=True)
    
    # Return to main settings
    async with AsyncSessionLocal() as db:
        group = await _get_group_by_id(target_group_id, db)
        
    await callback.message.edit_text(
        f"⚙️ <b>הגדרות קבוצה: {group.title}</b>\n\nבחר אפשרות:",
        parse_mode="HTML",
        reply_markup=main_settings_kb(target_group_id),
    )
