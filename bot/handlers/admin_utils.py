"""Shared utilities for admin registration and settings-edit tracking."""
from datetime import datetime, timezone

from sqlalchemy import select

from database.models import Group, GroupAdmin


async def register_group_admin(
    group_id: int,
    user_id: int,
    username: str | None,
    first_name: str | None,
    db,
) -> None:
    """Record a Telegram admin as having interacted with this group's settings."""
    result = await db.execute(
        select(GroupAdmin).where(
            GroupAdmin.group_id == group_id,
            GroupAdmin.admin_user_id == user_id,
        )
    )
    existing = result.scalar_one_or_none()
    if existing:
        # Update name in case it changed
        existing.admin_username = username
        existing.admin_first_name = first_name
    else:
        db.add(GroupAdmin(
            group_id=group_id,
            admin_user_id=user_id,
            admin_username=username,
            admin_first_name=first_name,
        ))


async def mark_settings_edited(
    group_id: int,
    user_id: int,
    username: str | None,
    first_name: str | None,
    db,
) -> None:
    """Stamp the group with who made the most recent settings change."""
    group = await db.get(Group, group_id)
    if not group:
        return
    display = (f"@{username}" if username else None) or first_name or str(user_id)
    group.settings_last_edited_by_id = user_id
    group.settings_last_edited_by_name = display
    group.settings_last_edited_at = datetime.now(timezone.utc)
