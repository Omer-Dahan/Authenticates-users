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
            await db.delete(q)
            await db.commit()
            await callback.answer("נמחק.")
        else:
            await callback.answer("לא נמצא.")

    await _show_questions(callback, group_id)


# ─── Smart add/edit flow ──────────────────────────────────────────────────

async def _show_editor_menu(message_or_callback, state: FSMContext) -> None:
    data = await state.get_data()
    group_id = data["group_id"]
    editing_q_id = data.get("editing_q_id")
    
    name = data.get("name") or "ללא שם"
    question = data.get("question") or "ריק"
    answers = ", ".join(data.get("accepted_answers", []))
    attempts = data.get("max_attempts", 3)
    timeout = data.get("timeout_seconds", 300)
    ban_text = "כן" if data.get("ban_on_fail") else "לא"
    score_pass = data.get("score_on_pass", 100.0)
    score_fail = data.get("score_on_fail", -100.0)
    
    mode_title = "✏️ עריכת שאלת אימות" if editing_q_id else "❓ שאלת אימות חדשה"
    
    text = (
        f"<b>{mode_title}</b>\n\n"
        f"🏷️ <b>שם השאלה:</b> {name}\n"
        f"📝 <b>טקסט השאלה:</b>\n{question}\n\n"
        f"🔑 <b>תשובות מקובלות:</b> {answers}\n"
        f"🔢 <b>ניסיונות מותרים:</b> {attempts}\n"
        f"⏱️ <b>זמן מענה:</b> {timeout} שניות\n"
        f"🚫 <b>חסימה בכישלון:</b> {ban_text}\n"
        f"🟢 <b>ניקוד (הצלחה):</b> {score_pass:+.0f}\n"
        f"🔴 <b>ניקוד (כישלון):</b> {score_fail:+.0f}\n\n"
        f"לחץ על הכפתורים מטה כדי לערוך שדות ספציפיים, ולבסוף לחץ על <b>שמור</b>."
    )
    
    kb = question_editor_kb(group_id, data)
    
    if isinstance(message_or_callback, CallbackQuery):
        await message_or_callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await message_or_callback.answer(text, parse_mode="HTML", reply_markup=kb)


@router.callback_query(F.data.startswith("q:add:"))
async def cb_question_add_start(callback: CallbackQuery, state: FSMContext) -> None:
    group_id = int(callback.data.split(":")[2])
    
    await state.set_state(QuestionEditState.menu)
    await state.update_data(
        group_id=group_id,
        editing_q_id=None,
        name="שאלה חדשה",
        question="מהו 2+2?",
        accepted_answers=["4"],
        max_attempts=3,
        timeout_seconds=300,
        ban_on_fail=False,
        score_on_pass=100.0,
        score_on_fail=-100.0,
    )
    
    await _show_editor_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("q:edit:"))
async def cb_question_edit(callback: CallbackQuery, state: FSMContext) -> None:
    parts = callback.data.split(":")
    group_id = int(parts[2])
    q_id = int(parts[3])
    
    async with AsyncSessionLocal() as db:
        q = await db.get(GroupQuestion, q_id)
        if not q or q.group_id != group_id:
            await callback.answer("שאלה לא נמצאה.")
            return
        
        await state.set_state(QuestionEditState.menu)
        await state.update_data(
            group_id=group_id,
            editing_q_id=q_id,
            name=q.name,
            question=q.question,
            accepted_answers=q.accepted_answers,
            max_attempts=q.max_attempts,
            timeout_seconds=q.timeout_seconds,
            ban_on_fail=q.ban_on_fail,
            score_on_pass=q.score_on_pass,
            score_on_fail=q.score_on_fail,
        )
    
    await _show_editor_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data.startswith("q_edit:field:"), QuestionEditState.menu)
async def cb_question_edit_select_field(callback: CallbackQuery, state: FSMContext) -> None:
    field = callback.data.split(":")[2]
    data = await state.get_data()
    
    if field == "name":
        await state.set_state(QuestionEditState.entering_name)
        await callback.message.edit_text(
            "🏷️ <b>עריכת שם השאלה</b>\n\n"
            f"השם הנוכחי: <code>{data.get('name', '')}</code>\n\n"
            "שלח כעת הודעה חדשה עם השם הרצוי:",
            parse_mode="HTML",
            reply_markup=cancel_field_edit_kb()
        )
    elif field == "question":
        await state.set_state(QuestionEditState.entering_question)
        await callback.message.edit_text(
            "📝 <b>עריכת טקסט השאלה</b>\n\n"
            f"הטקסט הנוכחי:\n<code>{data.get('question', '')}</code>\n\n"
            "שלח כעת הודעה חדשה עם טקסט השאלה הרצוי:",
            parse_mode="HTML",
            reply_markup=cancel_field_edit_kb()
        )
    elif field == "answers":
        await state.set_state(QuestionEditState.entering_answers)
        answers_str = ", ".join(data.get("accepted_answers", []))
        await callback.message.edit_text(
            "🔑 <b>עריכת תשובות מקובלות</b>\n\n"
            f"התשובות הנוכחיות: <code>{answers_str}</code>\n\n"
            "שלח כעת הודעה חדשה עם התשובות מופרדות בפסיקים:\n"
            "<i>לדוגמה: שלום, Shalom, hello</i>",
            parse_mode="HTML",
            reply_markup=cancel_field_edit_kb()
        )
    elif field == "attempts":
        await state.set_state(QuestionEditState.entering_attempts)
        await callback.message.edit_text(
            "🔢 <b>עריכת מספר ניסיונות מותרים</b>\n\n"
            f"הערך הנוכחי: <code>{data.get('max_attempts', 3)}</code>\n\n"
            "שלח כעת הודעה חדשה עם מספר הניסיונות הרצוי (מספר שלם):",
            parse_mode="HTML",
            reply_markup=cancel_field_edit_kb()
        )
    elif field == "timeout":
        await state.set_state(QuestionEditState.entering_timeout)
        await callback.message.edit_text(
            "⏱️ <b>עריכת זמן מענה (בשניות)</b>\n\n"
            f"הערך הנוכחי: <code>{data.get('timeout_seconds', 300)}</code>\n\n"
            "שלח כעת הודעה חדשה עם הזמן הרצוי בשניות (מספר שלם):",
            parse_mode="HTML",
            reply_markup=cancel_field_edit_kb()
        )
    elif field == "score_pass":
        await state.set_state(QuestionEditState.entering_score_pass)
        await callback.message.edit_text(
            "🟢 <b>עריכת ניקוד על תשובה נכונה</b>\n\n"
            f"הערך הנוכחי: <code>{data.get('score_on_pass', 100.0)}</code>\n\n"
            "שלח כעת הודעה חדשה עם הניקוד הרצוי:",
            parse_mode="HTML",
            reply_markup=cancel_field_edit_kb()
        )
    elif field == "score_fail":
        await state.set_state(QuestionEditState.entering_score_fail)
        await callback.message.edit_text(
            "🔴 <b>עריכת ניקוד על תשובה שגויה</b>\n\n"
            f"הערך הנוכחי: <code>{data.get('score_on_fail', -100.0)}</code>\n\n"
            "שלח כעת הודעה חדשה עם הניקוד הרצוי:",
            parse_mode="HTML",
            reply_markup=cancel_field_edit_kb()
        )
    
    await callback.answer()


@router.callback_query(F.data == "q_edit:toggle:ban", QuestionEditState.menu)
async def cb_question_edit_toggle_ban(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    current = data.get("ban_on_fail", False)
    await state.update_data(ban_on_fail=not current)
    await _show_editor_menu(callback, state)
    await callback.answer("שונה")


@router.callback_query(F.data.startswith("q_edit:action:"), QuestionEditState.menu)
async def cb_question_edit_action(callback: CallbackQuery, state: FSMContext) -> None:
    action = callback.data.split(":")[2]
    data = await state.get_data()
    group_id = data["group_id"]
    editing_q_id = data.get("editing_q_id")
    
    if action == "save":
        async with AsyncSessionLocal() as db:
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
                    q.score_on_fail = data["score_on_fail"]
                    await db.commit()
                    await callback.answer("✅ השינויים נשמרו בהצלחה.")
            else:
                q = GroupQuestion(
                    group_id=group_id,
                    name=data.get("name"),
                    question=data["question"],
                    accepted_answers=data["accepted_answers"],
                    validation_type="exact_match",
                    case_sensitive=False,
                    max_attempts=data["max_attempts"],
                    timeout_seconds=data["timeout_seconds"],
                    ban_on_fail=data["ban_on_fail"],
                    score_on_pass=data["score_on_pass"],
                    score_on_fail=data["score_on_fail"],
                    enabled=True,
                )
                db.add(q)
                await db.commit()
                editing_q_id = q.id
                await callback.answer("✅ שאלה חדשה נוצרה בהצלחה.")
        
        await state.clear()
        if editing_q_id:
            await _show_question_details(callback, group_id, editing_q_id)
        else:
            await _show_questions(callback, group_id)
            
    elif action == "cancel":
        await state.clear()
        await callback.answer("העריכה בוטלה.")
        if editing_q_id:
            await _show_question_details(callback, group_id, editing_q_id)
        else:
            await _show_questions(callback, group_id)


@router.callback_query(F.data == "q_edit:field_cancel")
async def cb_cancel_field_edit(callback: CallbackQuery, state: FSMContext) -> None:
    current_state = await state.get_state()
    if current_state in [
        QuestionEditState.entering_name.state,
        QuestionEditState.entering_question.state,
        QuestionEditState.entering_answers.state,
        QuestionEditState.entering_attempts.state,
        QuestionEditState.entering_timeout.state,
        QuestionEditState.entering_score_pass.state,
        QuestionEditState.entering_score_fail.state,
    ]:
        await state.set_state(QuestionEditState.menu)
        await _show_editor_menu(callback, state)
    await callback.answer()


@router.message(QuestionEditState.entering_name, F.chat.type == "private")
async def fsm_q_edit_name(message: Message, state: FSMContext) -> None:
    await state.update_data(name=message.text.strip())
    await state.set_state(QuestionEditState.menu)
    await _show_editor_menu(message, state)


@router.message(QuestionEditState.entering_question, F.chat.type == "private")
async def fsm_q_edit_question(message: Message, state: FSMContext) -> None:
    await state.update_data(question=message.text.strip())
    await state.set_state(QuestionEditState.menu)
    await _show_editor_menu(message, state)


@router.message(QuestionEditState.entering_answers, F.chat.type == "private")
async def fsm_q_edit_answers(message: Message, state: FSMContext) -> None:
    answers = [a.strip() for a in message.text.split(",") if a.strip()]
    if not answers:
        await message.answer("❌ אנא הזן לפחות תשובה אחת (מופרדות בפסיקים):", reply_markup=cancel_field_edit_kb())
        return
    await state.update_data(accepted_answers=answers)
    await state.set_state(QuestionEditState.menu)
    await _show_editor_menu(message, state)


@router.message(QuestionEditState.entering_attempts, F.chat.type == "private")
async def fsm_q_edit_attempts(message: Message, state: FSMContext) -> None:
    try:
        attempts = int(message.text.strip())
        if attempts <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ הכנס מספר שלם חיובי של ניסיונות. נסה שנית:", reply_markup=cancel_field_edit_kb())
        return
    await state.update_data(max_attempts=attempts)
    await state.set_state(QuestionEditState.menu)
    await _show_editor_menu(message, state)


@router.message(QuestionEditState.entering_timeout, F.chat.type == "private")
async def fsm_q_edit_timeout(message: Message, state: FSMContext) -> None:
    try:
        timeout = int(message.text.strip())
        if timeout <= 0:
            raise ValueError()
    except ValueError:
        await message.answer("❌ הכנס מספר שלם חיובי בשניות. נסה שנית:", reply_markup=cancel_field_edit_kb())
        return
    await state.update_data(timeout_seconds=timeout)
    await state.set_state(QuestionEditState.menu)
    await _show_editor_menu(message, state)


@router.message(QuestionEditState.entering_score_pass, F.chat.type == "private")
async def fsm_q_edit_score_pass(message: Message, state: FSMContext) -> None:
    try:
        score = float(message.text.strip())
    except ValueError:
        await message.answer("❌ הכנס מספר. נסה שנית:", reply_markup=cancel_field_edit_kb())
        return
    await state.update_data(score_on_pass=score)
    await state.set_state(QuestionEditState.menu)
    await _show_editor_menu(message, state)


@router.message(QuestionEditState.entering_score_fail, F.chat.type == "private")
async def fsm_q_edit_score_fail(message: Message, state: FSMContext) -> None:
    try:
        score = float(message.text.strip())
    except ValueError:
        await message.answer("❌ הכנס מספר. נסה שנית:", reply_markup=cancel_field_edit_kb())
        return
    await state.update_data(score_on_fail=score)
    await state.set_state(QuestionEditState.menu)
    await _show_editor_menu(message, state)
