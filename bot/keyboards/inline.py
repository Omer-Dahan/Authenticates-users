"""Inline keyboard builders for admin controls."""
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


def build_admin_review_keyboard(user_id: int, join_request_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✅ Approve",
            callback_data=f"admin:approve:{user_id}:{join_request_id}",
        ),
        InlineKeyboardButton(
            text="❌ Reject",
            callback_data=f"admin:reject:{user_id}:{join_request_id}",
        ),
    )
    builder.row(
        InlineKeyboardButton(
            text="🔨 Ban",
            callback_data=f"admin:ban:{user_id}:{join_request_id}",
        ),
        InlineKeyboardButton(
            text="ℹ️ Info",
            callback_data=f"admin:info:{user_id}:{join_request_id}",
        ),
    )
    return builder.as_markup()


def build_verification_keyboard(user_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📝 Submit Answer",
            callback_data=f"verify:start:{user_id}",
        )
    )
    return builder.as_markup()


def build_security_mode_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🟢 Normal", callback_data="security:normal"),
        InlineKeyboardButton(text="🟡 Strict", callback_data="security:strict"),
        InlineKeyboardButton(text="🔴 Lockdown", callback_data="security:lockdown"),
    )
    return builder.as_markup()
