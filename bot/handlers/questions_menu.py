"""FSM-based verification question management through the bot."""
from aiogram import Router, F
from aiogram.types import CallbackQuery, Message
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import GroupQuestion
from bot.keyboards.settings_kb import (
    questions_list_kb, yes_no_kb, back_to_main_kb, question_details_kb,
    question_editor_kb, cancel_field_edit_kb
)
from logs import get_logger

logger = get_logger(__name__)
router = Router()


class QuestionEditState(StatesGroup):
    menu = State()
    entering_name = State()
    entering_question = State()
    entering_answers = State()
    entering_attempts = State()
    entering_timeout = State()
    entering_score_pass = State()
    entering_score_fail = State()


async def _show_questions(callback: CallbackQuery, group_id: int) -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(GroupQuestion).where(GroupQuestion.group_id == group_id).order_by(GroupQuestion.created_at)
        )
        questions = result.scalars().all()

    kb = questions_list_kb(group_id, questions)
    await callback.message.edit_text(
        f"❓ <b>שאלות אימות ({len(questions)})</b>\n"
        f"שאלות המוצגות למצטרפים החדשים. מענה נכון מעניק ניקוד חיובי, ומענה שגוי או התעלמות מורידים ניקוד.\n\n"
        f"שאלות נבחרות אקראית לכל בקשת הצטרפות:",
        parse_mode="HTML",
        reply_markup=kb,
    )


# ─── Show questions list ──────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("sm:questions:"))
async def cb_questions_menu(callback: CallbackQuery) -> None:
    group_id = int(callback.data.split(":")[2])
    await _show_questions(callback, group_id)
    await callback.answer()


# ─── Toggle ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("q:toggle:"))
async def cb_question_toggle(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    q_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        q = await db.get(GroupQuestion, q_id)
        if not q or q.group_id != group_id:
            await callback.answer("שאלה לא נמצאה.")
            return
        q.enabled = not q.enabled
        await db.commit()
        await callback.answer("הופעל" if q.enabled else "כובה")

    await _show_questions(callback, group_id)


# ─── Delete ───────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("q:delete:"))
async def cb_question_delete(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    q_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        q = await db.get(GroupQuestion, q_id)
        if q and q.group_id == group_id:
            await db.delete(q)
            await db.commit()
            await callback.answer("נמחק.")
        else:
            await callback.answer("לא נמצא.")

    await _show_questions(callback, group_id)


# ─── Start add-question flow ──────────────────────────────────────────────────

@router.callback_query(F.data.startswith("q:add:"))
async def cb_question_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[2])
    await state.set_state(AddQuestionState.entering_name)
    await state.update_data(group_id=group_id, editing_q_id=None)
    await callback.message.edit_text(
        "❓ <b>שאלת אימות חדשה</b>\n\nהזן שם קצר לשאלה (למשל: 'שלום'):",
        parse_mode="HTML",
    )
    await callback.answer()

@router.message(AddQuestionState.entering_name, F.chat.type == "private")
async def fsm_q_enter_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(AddQuestionState.entering_question)
    await message.answer(
        "הזן את טקסט השאלה המלא:",
        parse_mode="HTML",
    )


@router.message(AddQuestionState.entering_question, F.chat.type == "private")
async def fsm_q_enter_question(message: Message, state: FSMContext) -> None:
    await state.update_data(question=message.text.strip())
    await state.set_state(AddQuestionState.entering_answers)
    await message.answer(
        "הזן תשובות מקובלות, מופרדות בפסיק:\n"
        "<i>לדוגמה: שלום, Shalom, hello</i>",
        parse_mode="HTML",
    )


@router.message(AddQuestionState.entering_answers, F.chat.type == "private")
async def fsm_q_enter_answers(message: Message, state: FSMContext) -> None:
    answers = [a.strip() for a in message.text.split(",") if a.strip()]
    await state.update_data(accepted_answers=answers)
    await state.set_state(AddQuestionState.entering_attempts)
    await message.answer("מספר ניסיונות מותרים (ברירת מחדל: 3):")


@router.message(AddQuestionState.entering_attempts, F.chat.type == "private")
async def fsm_q_enter_attempts(message: Message, state: FSMContext) -> None:
    try:
        attempts = int(message.text.strip())
    except ValueError:
        await message.answer("❌ הכנס מספר שלם. נסה שנית:")
        return
    await state.update_data(max_attempts=attempts)
    await state.set_state(AddQuestionState.entering_timeout)
    await message.answer("זמן מוגבל לתשובה בשניות (ברירת מחדל: 300):")


@router.message(AddQuestionState.entering_timeout, F.chat.type == "private")
async def fsm_q_enter_timeout(message: Message, state: FSMContext) -> None:
    try:
        timeout = int(message.text.strip())
    except ValueError:
        await message.answer("❌ הכנס מספר שלם בשניות. נסה שנית:")
        return
    data = await state.get_data()
    await state.update_data(timeout_seconds=timeout)
    await state.set_state(AddQuestionState.asking_ban_on_fail)
    await message.answer(
        "לחסום משתמש אם נכשל?",
        reply_markup=yes_no_kb(data["group_id"], "q"),
    )


@router.callback_query(F.data.startswith("q:yn:"), AddQuestionState.asking_ban_on_fail)
async def fsm_q_ban_on_fail(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    ban = parts[3] == "yes"
    await state.update_data(ban_on_fail=ban)
    await state.set_state(AddQuestionState.entering_score_pass)
    await callback.message.edit_text("ניקוד על תשובה נכונה (ברירת מחדל: 100):")
    await callback.answer()


@router.message(AddQuestionState.entering_score_pass, F.chat.type == "private")
async def fsm_q_score_pass(message: Message, state: FSMContext) -> None:
    try:
        score = float(message.text.strip())
    except ValueError:
        await message.answer("❌ הכנס מספר. נסה שנית:")
        return
    await state.update_data(score_on_pass=score)
    await state.set_state(AddQuestionState.entering_score_fail)
    await message.answer("ניקוד על תשובה שגויה (ברירת מחדל: -100):")


@router.message(AddQuestionState.entering_score_fail, F.chat.type == "private")
async def fsm_q_score_fail(message: Message, state: FSMContext) -> None:
    try:
        score = float(message.text.strip())
    except ValueError:
        await message.answer("❌ הכנס מספר. נסה שנית:")
        return

    data = await state.get_data()
    await state.clear()

    async with AsyncSessionLocal() as db:
        editing_q_id = data.get("editing_q_id")
        if editing_q_id:
            q = await db.get(GroupQuestion, editing_q_id)
            if q:
                q.name = data.get("name")
                q.question = data["question"]
                q.accepted_answers = data["accepted_answers"]
                q.max_attempts = data["max_attempts"]
                q.timeout_seconds = data["timeout_seconds"]
                q.ban_on_fail = data["ban_on_fail"]
                q.score_on_pass = data["score_on_pass"]
                q.score_on_fail = score
                await db.commit()
                await message.answer("✅ השאלה עודכנה בהצלחה.")
        else:
            db.add(GroupQuestion(
                group_id=data["group_id"],
                name=data.get("name"),
                question=data["question"],
                accepted_answers=data["accepted_answers"],
                validation_type="exact_match",
                case_sensitive=False,
                max_attempts=data["max_attempts"],
                timeout_seconds=data["timeout_seconds"],
                ban_on_fail=data["ban_on_fail"],
                score_on_pass=data["score_on_pass"],
                score_on_fail=score,
                enabled=True,
            ))
        await db.commit()

    await message.answer(
        "✅ <b>שאלה נשמרה!</b>\n\n"
        f"❓ {data['question']}\n"
        f"✔️ תשובות: {', '.join(data['accepted_answers'])}\n"
        f"🔢 ניסיונות: {data['max_attempts']} | ⏱ {data['timeout_seconds']}ש\n"
        f"📊 עובר: {data['score_on_pass']:+.0f} | נכשל: {score:+.0f}",
        parse_mode="HTML",
        reply_markup=back_to_main_kb(data["group_id"]),
    )


# ─── Detailed View & Edit ─────────────────────────────────────────────────────

async def _show_question_details(callback: CallbackQuery, group_id: int, q_id: int) -> None:
    async with AsyncSessionLocal() as db:
        q = await db.get(GroupQuestion, q_id)
        if not q or q.group_id != group_id:
            await callback.answer("שאלה לא נמצאה.")
            return

        status = "✅ פעיל" if q.enabled else "❌ מושהה"
        ban_text = "כן" if q.ban_on_fail else "לא"
        name_text = q.name if q.name else "ללא שם"

        text = (
            f"❓ <b>שם השאלה:</b> {name_text}\n"
            f"<b>סטטוס:</b> {status}\n\n"
            f"<b>טקסט השאלה:</b>\n{q.question}\n\n"
            f"<b>תשובות מקובלות:</b> {', '.join(q.accepted_answers)}\n"
            f"<b>ניסיונות מותרים:</b> {q.max_attempts}\n"
            f"<b>זמן מענה:</b> {q.timeout_seconds} שניות\n"
            f"<b>חסימה בכישלון:</b> {ban_text}\n"
            f"<b>ניקוד (הצלחה):</b> {q.score_on_pass:+.0f}\n"
            f"<b>ניקוד (כישלון):</b> {q.score_on_fail:+.0f}"
        )
        kb = question_details_kb(group_id, q_id, q.enabled)
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("q:view:"))
async def cb_question_view(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    q_id = int(parts[3])
    await _show_question_details(callback, group_id, q_id)
    await callback.answer()


@router.callback_query(F.data.startswith("q:toggle_v:"))
async def cb_question_toggle_v(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    q_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        q = await db.get(GroupQuestion, q_id)
        if not q or q.group_id != group_id:
            await callback.answer("שאלה לא נמצאה.")
            return
        q.enabled = not q.enabled
        await db.commit()
        await callback.answer("הופעל" if q.enabled else "כובה")

    await _show_question_details(callback, group_id, q_id)


@router.callback_query(F.data.startswith("q:delete_v:"))
async def cb_question_delete_v(callback: CallbackQuery) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    q_id = int(parts[3])

    async with AsyncSessionLocal() as db:
        q = await db.get(GroupQuestion, q_id)
        if q and q.group_id == group_id:
            db.delete(q)
            await db.commit()
            await callback.answer("נמחק.")
        else:
            await callback.answer("לא נמצא.")

    await _show_questions(callback, group_id)


@router.callback_query(F.data.startswith("q:edit:"))
async def cb_question_edit(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    q_id = int(parts[3])
    
    await state.set_state(AddQuestionState.entering_name)
    await state.update_data(group_id=group_id, editing_q_id=q_id)
    await callback.message.edit_text(
        "✏️ <b>עריכת שאלת אימות</b>\n\nהזן שם קצר חדש לשאלה (למשל: 'שלום'):",
        parse_mode="HTML",
    )
    await callback.answer()
