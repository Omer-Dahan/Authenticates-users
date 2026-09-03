"""Inline keyboard builders for the settings menu system."""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def main_settings_kb(group_id: int) -> InlineKeyboardMarkup:
    g = group_id
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="📋 כללים", callback_data=f"sm:rules:{g}"),
        InlineKeyboardButton(text="❓ שאלות אימות", callback_data=f"sm:questions:{g}"),
    )
    builder.row(
        InlineKeyboardButton(text="🚫 רשימה שחורה", callback_data=f"sm:blacklist:{g}"),
        InlineKeyboardButton(text="✅ רשימה לבנה", callback_data=f"sm:whitelist:{g}"),
    )
    builder.row(
        InlineKeyboardButton(text="🛡️ מצב אבטחה", callback_data=f"sm:mode:{g}"),
        InlineKeyboardButton(text="📊 ספי ניקוד", callback_data=f"sm:thresholds:{g}"),
    )
    builder.row(
        InlineKeyboardButton(text="🌐 פילטרי שפה", callback_data=f"sm:languages:{g}"),
        InlineKeyboardButton(text="📈 סטטיסטיקות", callback_data=f"sm:stats:{g}"),
    )
    builder.row(
        InlineKeyboardButton(text="🔔 התראות סקירה ידנית", callback_data=f"sm:notify:{g}"),
    )
    builder.row(
        InlineKeyboardButton(text="🔄 ייבוא הגדרות", callback_data=f"sm:import:{g}"),
        InlineKeyboardButton(text="💾 קבע כברירת מחדל", callback_data=f"sm:set_default:{g}"),
    )
    builder.row(
        InlineKeyboardButton(text="◀️ חזרה לרשימת קבוצות", callback_data="start:refresh"),
    )
    return builder.as_markup()


def security_mode_kb(group_id: int) -> InlineKeyboardMarkup:
    g = group_id
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🟢 רגיל", callback_data=f"sm:set_mode:{g}:normal"),
        InlineKeyboardButton(text="🟡 קפדני", callback_data=f"sm:set_mode:{g}:strict"),
        InlineKeyboardButton(text="🔴 נעילה", callback_data=f"sm:set_mode:{g}:lockdown"),
    )
    builder.row(InlineKeyboardButton(text="◀️ חזרה להגדרות", callback_data=f"sm:main:{g}"))
    builder.row(InlineKeyboardButton(text="🏠 רשימת קבוצות", callback_data="start:refresh"))
    return builder.as_markup()


def back_to_main_kb(group_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ חזרה להגדרות", callback_data=f"sm:main:{group_id}"))
    builder.row(InlineKeyboardButton(text="🏠 רשימת קבוצות", callback_data="start:refresh"))
    return builder.as_markup()


def rules_list_kb(group_id: int, rules: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for rule in rules:
        status = "✅" if rule.enabled else "❌"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {rule.name} ({rule.score:+.0f})",
                callback_data=f"rule:toggle:{group_id}:{rule.id}",
            ),
            InlineKeyboardButton(
                text="🗑",
                callback_data=f"rule:delete:{group_id}:{rule.id}",
            ),
        )
    builder.row(InlineKeyboardButton(text="➕ כלל חדש", callback_data=f"rule:add:{group_id}"))
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data=f"sm:main:{group_id}"))
    return builder.as_markup()


def rule_type_kb(group_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Regex", callback_data=f"rule:type:{group_id}:regex"),
        InlineKeyboardButton(text="מילת מפתח", callback_data=f"rule:type:{group_id}:keyword"),
        InlineKeyboardButton(text="התאמה מדויקת", callback_data=f"rule:type:{group_id}:exact_match"),
    )
    builder.row(InlineKeyboardButton(text="❌ ביטול", callback_data=f"rule:cancel:{group_id}"))
    return builder.as_markup()


def rule_target_kb(group_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="שם מלא", callback_data=f"rule:target:{group_id}:full_name"),
        InlineKeyboardButton(text="שם פרטי", callback_data=f"rule:target:{group_id}:first_name"),
    )
    builder.row(
        InlineKeyboardButton(text="שם משפחה", callback_data=f"rule:target:{group_id}:last_name"),
        InlineKeyboardButton(text="יוזרנם", callback_data=f"rule:target:{group_id}:username"),
    )
    builder.row(InlineKeyboardButton(text="❌ ביטול", callback_data=f"rule:cancel:{group_id}"))
    return builder.as_markup()


def confirm_kb(group_id: int, prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="✅ שמור", callback_data=f"{prefix}:save:{group_id}"),
        InlineKeyboardButton(text="❌ ביטול", callback_data=f"{prefix}:cancel:{group_id}"),
    )
    return builder.as_markup()


def questions_list_kb(group_id: int, questions: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for q in questions:
        status = "✅" if q.enabled else "❌"
        # use name if available, fallback to short question
        display_name = q.name if q.name else q.question[:20] + ("…" if len(q.question) > 20 else "")
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {display_name}",
                callback_data=f"q:view:{group_id}:{q.id}",
            )
        )
    builder.row(InlineKeyboardButton(text="➕ שאלה חדשה", callback_data=f"q:add:{group_id}"))
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data=f"sm:main:{group_id}"))
    return builder.as_markup()


def question_details_kb(group_id: int, q_id: int, is_enabled: bool) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    toggle_text = "⏸ השהה" if is_enabled else "▶️ הפעל"
    builder.row(
        InlineKeyboardButton(text="✏️ ערוך", callback_data=f"q:edit:{group_id}:{q_id}"),
        InlineKeyboardButton(text=toggle_text, callback_data=f"q:toggle_v:{group_id}:{q_id}"),
        InlineKeyboardButton(text="🗑 מחק", callback_data=f"q:delete_v:{group_id}:{q_id}"),
    )
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data=f"sm:questions:{group_id}"))
    return builder.as_markup()


def question_editor_kb(data: dict) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()

    name = data.get("name") or "ללא שם"
    question = data.get("question") or "ריק"
    q_preview = question[:20] + "..." if len(question) > 20 else question
    answers = ", ".join(data.get("accepted_answers", []))
    ans_preview = answers[:20] + "..." if len(answers) > 20 else answers
    attempts = data.get("max_attempts", 3)
    timeout = data.get("timeout_seconds", 300)
    ban = "כן" if data.get("ban_on_fail") else "לא"
    score_pass = data.get("score_on_pass", 100.0)
    score_fail = data.get("score_on_fail", -100.0)

    builder.row(InlineKeyboardButton(text=f"🏷️ שם: {name}", callback_data="q_edit:field:name"))
    builder.row(InlineKeyboardButton(text=f"📝 שאלה: {q_preview}", callback_data="q_edit:field:question"))
    builder.row(InlineKeyboardButton(text=f"🔑 תשובות: {ans_preview}", callback_data="q_edit:field:answers"))

    builder.row(
        InlineKeyboardButton(text=f"🔢 ניסיונות: {attempts}", callback_data="q_edit:field:attempts"),
        InlineKeyboardButton(text=f"⏱️ זמן: {timeout}ש", callback_data="q_edit:field:timeout"),
    )
    builder.row(
        InlineKeyboardButton(text=f"🚫 חסימה בכישלון: {ban}", callback_data="q_edit:toggle:ban"),
    )
    builder.row(
        InlineKeyboardButton(text=f"🟢 עובר: {score_pass:+.0f}", callback_data="q_edit:field:score_pass"),
        InlineKeyboardButton(text=f"🔴 נכשל: {score_fail:+.0f}", callback_data="q_edit:field:score_fail"),
    )

    builder.row(
        InlineKeyboardButton(text="💾 שמור", callback_data="q_edit:action:save"),
        InlineKeyboardButton(text="❌ ביטול", callback_data="q_edit:action:cancel"),
    )

    return builder.as_markup()


def cancel_field_edit_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(InlineKeyboardButton(text="◀️ ביטול וחזרה", callback_data="q_edit:field_cancel"))
    return builder.as_markup()


def yes_no_kb(group_id: int, prefix: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="כן", callback_data=f"{prefix}:yn:{group_id}:yes"),
        InlineKeyboardButton(text="לא", callback_data=f"{prefix}:yn:{group_id}:no"),
    )
    return builder.as_markup()


def blacklist_list_kb(group_id: int, entries: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for entry in entries:
        builder.row(
            InlineKeyboardButton(
                text=f"🚫 {entry.keyword} ({entry.score:+.0f})",
                callback_data=f"bl:noop:{entry.id}",
            ),
            InlineKeyboardButton(text="🗑", callback_data=f"bl:delete:{group_id}:{entry.id}"),
        )
    builder.row(InlineKeyboardButton(text="➕ הוסף מילה", callback_data=f"bl:add:{group_id}"))
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data=f"sm:main:{group_id}"))
    return builder.as_markup()


def whitelist_list_kb(group_id: int, entries: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for entry in entries:
        name = entry.username or str(entry.telegram_id)
        builder.row(
            InlineKeyboardButton(text=f"✅ {name}", callback_data=f"wl:noop:{entry.id}"),
            InlineKeyboardButton(text="🗑", callback_data=f"wl:delete:{group_id}:{entry.id}"),
        )
    builder.row(InlineKeyboardButton(text="➕ הוסף משתמש", callback_data=f"wl:add:{group_id}"))
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data=f"sm:main:{group_id}"))
    return builder.as_markup()


def languages_list_kb(group_id: int, filters: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for lang in filters:
        status = "✅" if lang.enabled else "❌"
        builder.row(
            InlineKeyboardButton(
                text=f"{status} {lang.language} ({lang.score:+.0f})",
                callback_data=f"lang:toggle:{group_id}:{lang.id}",
            ),
        )
    builder.row(InlineKeyboardButton(text="◀️ חזרה", callback_data=f"sm:main:{group_id}"))
    return builder.as_markup()


def notify_settings_kb(group_id: int, enabled: bool) -> InlineKeyboardMarkup:
    g = group_id
    builder = InlineKeyboardBuilder()
    if enabled:
        builder.row(InlineKeyboardButton(text="🔕 כבה התראות", callback_data=f"sm:set_notify:{g}:off"))
    else:
        builder.row(InlineKeyboardButton(text="🔔 הפעל התראות", callback_data=f"sm:set_notify:{g}:on"))
    builder.row(InlineKeyboardButton(text="◀️ חזרה להגדרות", callback_data=f"sm:main:{g}"))
    builder.row(InlineKeyboardButton(text="🏠 רשימת קבוצות", callback_data="start:refresh"))
    return builder.as_markup()


def admin_review_kb(user_id: int, join_request_id: int, group_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ אשר",
            callback_data=f"rev:approve:{user_id}:{join_request_id}:{group_id}",
        ),
        InlineKeyboardButton(
            text="❌ דחה",
            callback_data=f"rev:reject:{user_id}:{join_request_id}:{group_id}",
        ),
        InlineKeyboardButton(
            text="🔨 חסום",
            callback_data=f"rev:ban:{user_id}:{join_request_id}:{group_id}",
        ),
    )
    return builder.as_markup()


def import_list_kb(target_group_id: int, other_groups: list) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for g in other_groups:
        title = g.title or str(g.chat_id)
        builder.row(
            InlineKeyboardButton(
                text=title,
                callback_data=f"sm:imp_from:{target_group_id}:{g.id}"
            )
        )
    builder.row(InlineKeyboardButton(text="◀️ חזרה להגדרות", callback_data=f"sm:main:{target_group_id}"))
    return builder.as_markup()


def import_confirm_kb(target_group_id: int, source_group_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ כן, יבא ודרוס הגדרות",
            callback_data=f"sm:imp_conf:{target_group_id}:{source_group_id}"
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="❌ ביטול",
            callback_data=f"sm:import:{target_group_id}"
        ),
    )
    return builder.as_markup()
