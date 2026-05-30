"""Middleware: skip unregistered or banned groups on join request events."""
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import ChatJoinRequest
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import Group
from logs import get_logger

logger = get_logger(__name__)


class GroupCheckMiddleware(BaseMiddleware):
    """Only processes chat_join_request events for registered, active, non-banned groups."""

    async def __call__(
        self,
        handler: Callable[[ChatJoinRequest, Dict[str, Any]], Awaitable[Any]],
        event: ChatJoinRequest,
        data: Dict[str, Any],
    ) -> Any:
        chat_id = event.chat.id

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(Group).where(Group.chat_id == chat_id)
            )
            group = result.scalar_one_or_none()

        if group is None:
            logger.debug("Join request ignored — group not registered", chat_id=chat_id)
            return

        if group.is_banned:
            logger.debug("Join request ignored — group is banned", chat_id=chat_id)
            return

        if not group.is_active:
            logger.debug("Join request ignored — group is inactive", chat_id=chat_id)
            return

        data["group_db"] = group
        return await handler(event, data)
