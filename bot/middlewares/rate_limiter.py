"""Rate limiting and anti-raid middleware."""
import time
from collections import defaultdict
from typing import Any, Awaitable, Callable, Dict, List
from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, ChatJoinRequest
from logs import get_logger

logger = get_logger(__name__)


class RateLimitMiddleware(BaseMiddleware):
    """Limits actions per user within a time window."""

    def __init__(self, max_requests: int = 10, window_seconds: int = 60) -> None:
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._user_actions: Dict[int, List[float]] = defaultdict(list)

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user_id = self._extract_user_id(event)
        if user_id:
            now = time.monotonic()
            window_start = now - self.window_seconds
            actions = self._user_actions[user_id]
            # Prune old entries
            self._user_actions[user_id] = [t for t in actions if t > window_start]

            if len(self._user_actions[user_id]) >= self.max_requests:
                logger.warning("Rate limit exceeded", user_id=user_id)
                return  # Drop the update silently

            self._user_actions[user_id].append(now)

        return await handler(event, data)

    @staticmethod
    def _extract_user_id(event: TelegramObject) -> int | None:
        if isinstance(event, ChatJoinRequest) and event.from_user:
            return event.from_user.id
        return None


class RaidProtectionMiddleware(BaseMiddleware):
    """Detects sudden spikes in join requests (raid mode)."""

    def __init__(self, threshold: int = 10, window_seconds: int = 30) -> None:
        self.threshold = threshold
        self.window_seconds = window_seconds
        self._join_times: List[float] = []
        self.raid_active = False

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, ChatJoinRequest):
            now = time.monotonic()
            window_start = now - self.window_seconds
            self._join_times = [t for t in self._join_times if t > window_start]
            self._join_times.append(now)

            if len(self._join_times) >= self.threshold:
                if not self.raid_active:
                    self.raid_active = True
                    logger.warning(
                        "RAID DETECTED",
                        join_count=len(self._join_times),
                        window=self.window_seconds,
                    )
            else:
                self.raid_active = False

            data["raid_active"] = self.raid_active

        return await handler(event, data)
