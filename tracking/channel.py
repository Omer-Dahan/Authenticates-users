"""Send every moderation decision to the tracking channel."""
from datetime import datetime, timezone
from typing import Optional, List

from aiogram import Bot
from database.models import DecisionEnum
from config import settings
from logs import get_logger

logger = get_logger(__name__)

_DECISION_ICON = {
    DecisionEnum.approved: "✅ אושר",
    DecisionEnum.rejected: "❌ נדחה",
    DecisionEnum.banned: "🔨 הוגבל",
    DecisionEnum.manual_review: "👀 סקירה ידנית",
    DecisionEnum.pending: "⏳ ממתין",
}


def _format_rules(matched_rules: List[dict]) -> str:
    if not matched_rules:
        return "—"
    parts = []
    for r in matched_rules:
        score = r.get("score", 0)
        sign = "+" if score >= 0 else ""
        parts.append(f"{r.get('rule_id', '?')} ({sign}{score:.0f})")
    return ", ".join(parts)


async def send_decision(
    bot: Bot,
    decision: DecisionEnum,
    user_id: int,
    user_name: str,
    username: Optional[str],
    total_score: float,
    matched_rules: List[dict],
    group_title: str,
    group_username: Optional[str],
    reason: Optional[str] = None,
) -> None:
    if not settings.tracking_channel_id:
        return

    icon = _DECISION_ICON.get(decision, "❓")
    group_str = f"{group_title}"
    if group_username:
        group_str += f" (@{group_username})"

    username_str = f"@{username}" if username else "—"
    now = datetime.now(timezone.utc).strftime("%d/%m/%Y %H:%M:%S")

    if decision == DecisionEnum.banned and reason:
        text = (
            f"{icon} | 📍 {group_str}\n"
            f"👤 {user_name} | ID: <code>{user_id}</code>\n"
            f"📊 ניקוד: {total_score:+.0f}\n"
            f"⚡ סיבה: {reason}\n"
            f"🕐 {now}\n"
            f"─────────────────────────"
        )
    else:
        rules_str = _format_rules(matched_rules)
        text = (
            f"{icon} | 📍 {group_str}\n"
            f"👤 {user_name} ({username_str}) | ID: <code>{user_id}</code>\n"
            f"📊 ניקוד: {total_score:+.0f}\n"
            f"🔍 כללים שהתאימו: {rules_str}\n"
            f"🕐 {now}\n"
            f"─────────────────────────"
        )

    try:
        await bot.send_message(settings.tracking_channel_id, text, parse_mode="HTML")
    except Exception as e:
        logger.error("Failed to send tracking message", error=str(e))
