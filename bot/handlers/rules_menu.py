"""FSM-based rule management through the bot (add / toggle / delete)."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import GroupRule, RuleTypeEnum, RuleTargetEnum
from bot.keyboards.settings_kb import rules_list_kb, rule_type_kb, rule_target_kb, back_to_main_kb
from bot.handlers.admin_utils import mark_settings_edited
from logs import get_logger

logger = get_logger(__name__)
router = Router()


class AddRuleState(StatesGroup):
    choosing_type = State()
    choosing_target = State()
    entering_pattern = State()
    entering_score = State()


async def _show_rules(query_or_msg, group_id: int, edit: bool = True) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupRule).where(GroupRule.group_id == group_id).order_by(GroupRule.created_at)
        )
        rules = result.scalars().all()

    kb = rules_list_kb(group_id, rules)
    text = (
        f"📋 <b>כללים ({len(rules)})</b>\n"
        f"כללים לסינון משתמשים לפי תבניות טקסט (Regex, מילות מפתח או התאמה מלאה) בשמותיהם ובכינוייהם.\n\n"
        f"בחר כלל לשינוי סטטוס / מחיקה, או הוסף כלל חדש:"
    )

    if edit:
        await query_or_msg.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await query_or_msg.answer(text, parse_mode="HTML", reply_markup=kb)


# ─── Show rules list ──────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:rules:"))
async def cb_rules_menu(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    await _show_rules(callback, group_id, edit=True)
    await callback.answer()


# ─── Toggle rule enabled/disabled ────────────────────────────────────────────

@router.callback_query(F.data.startswith("rule:toggle:"))
async def cb_rule_toggle(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    rule_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        rule = await db.get(GroupRule, rule_id)
        if not rule or rule.group_id != group_id:
            await callback.answer("כלל לא נמצא.")
            return
        rule.enabled = not rule.enabled
        await mark_settings_edited(group_id, callback.from_user.id, callback.from_user.username, callback.from_user.first_name, db)
        await db.commit()
        state = "הופעל" if rule.enabled else "כובה"
        await callback.answer(f"כלל '{rule.name}' {state}")

    await _show_rules(callback, group_id, edit=True)


# ─── Delete rule ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("rule:delete:"))
async def cb_rule_delete(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    rule_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        rule = await db.get(GroupRule, rule_id)
        if rule and rule.group_id == group_id:
            await db.delete(rule)
            await mark_settings_edited(group_id, callback.from_user.id, callback.from_user.username, callback.from_user.first_name, db)
            await db.commit()
            await callback.answer("הכלל נמחק.")
        else:
            await callback.answer("כלל לא נמצא.")

    await _show_rules(callback, group_id, edit=True)


# ─── Start add-rule flow ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("rule:add:"))
async def cb_rule_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[2])
    await state.set_state(AddRuleState.choosing_type)
    await state.update_data(group_id=group_id)
    await callback.message.edit_text(
        "➕ <b>כלל חדש</b>\n\nבחר סוג כלל:",
        parse_mode="HTML",
        reply_markup=rule_type_kb(group_id),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rule:cancel:"))
async def cb_rule_cancel(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[2])
    await state.clear()
    await _show_rules(callback, group_id, edit=True)
    await callback.answer("בוטל")


# ─── Choose type ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("rule:type:"), AddRuleState.choosing_type)
async def cb_rule_choose_type(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    rule_type = parts[3]

    await state.update_data(rule_type=rule_type)
    await state.set_state(AddRuleState.choosing_target)
    await callback.message.edit_text(
        f"➕ סוג: <b>{rule_type}</b>\n\nבחר יעד:",
        parse_mode="HTML",
        reply_markup=rule_target_kb(group_id),
    )
    await callback.answer()


# ─── Choose target ────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("rule:target:"), AddRuleState.choosing_target)
async def cb_rule_choose_target(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    target = parts[3]

    data = await state.get_data()
    rule_type = data.get("rule_type", "regex")
    await state.update_data(target=target)

    if rule_type == "keyword":
        await state.set_state(AddRuleState.entering_pattern)
        prompt = "הזן מילות מפתח מופרדות בפסיקים (לדוגמה: spam,casino,crypto):"
    else:
        await state.set_state(AddRuleState.entering_pattern)
        prompt = "הזן את הביטוי הרגולרי / הטקסט המדויק:"

    await callback.message.edit_text(
        f"➕ סוג: <b>{rule_type}</b> | יעד: <b>{target}</b>\n\n{prompt}",
        parse_mode="HTML",
    )
    await callback.answer()


# ─── Enter pattern ────────────────────────────────────────────────────────────

@router.message(AddRuleState.entering_pattern, F.chat.type == "private")
async def fsm_rule_enter_pattern(message: Message, state: FSMContext) -> None:
    await state.update_data(pattern=message.text.strip())
    await state.set_state(AddRuleState.entering_score)
    await message.answer("הזן ניקוד (מספר חיובי לאישור, שלילי לדחייה, לדוגמה: 70 או -80):")


# ─── Enter score ──────────────────────────────────────────────────────────────

@router.message(AddRuleState.entering_score, F.chat.type == "private")
async def fsm_rule_enter_score(message: Message, state: FSMContext) -> None:
    try:
        score = float(message.text.strip())
    except ValueError:
        await message.answer("❌ הניקוד חייב להיות מספר. נסה שנית:")
        return

    data = await state.get_data()
    group_id = data["group_id"]
    rule_type = data["rule_type"]
    target = data["target"]
    pattern_raw = data["pattern"]

    keywords = []
    pattern = None

    if rule_type == "keyword":
        keywords = [k.strip() for k in pattern_raw.split(",") if k.strip()]
        name = f"מילות מפתח: {', '.join(keywords[:3])}"
    else:
        pattern = pattern_raw
        name = f"{rule_type}: {pattern[:30]}"

    async with AsyncSessionLocal() as db:
        existing = await db.execute(
            select(GroupRule).where(GroupRule.group_id == group_id)
        )
        count = len(existing.scalars().all())
        rule_id = f"custom_{count + 1}"

        db.add(GroupRule(
            group_id=group_id,
            rule_id=rule_id,
            name=name,
            enabled=True,
            rule_type=rule_type,
            target=target,
            pattern=pattern,
            keywords=keywords,
            score=score,
        ))
        await mark_settings_edited(group_id, message.from_user.id, message.from_user.username, message.from_user.first_name, db)
        await db.commit()

    await state.clear()
    sign = "+" if score >= 0 else ""
    await message.answer(
        f"✅ <b>כלל נשמר!</b>\n\n"
        f"🆔 {rule_id}\n"
        f"📌 {rule_type} על {target}\n"
        f"📝 {pattern_raw[:50]}\n"
        f"📊 ניקוד: {sign}{score:.0f}",
        parse_mode="HTML",
        reply_markup=back_to_main_kb(group_id),
    )
