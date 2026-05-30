"""Bot entry point — multi-tenant setup."""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# pylint: disable=wrong-import-position
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramAPIError
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from config import settings
from logs import get_logger, setup_logging
from database.init_db import init_db
from moderation import ModerationEngine
from verification import VerificationEngine, start_cleanup_loop

from bot.handlers import join_requests, verification, setup, settings_menu
from bot.handlers import rules_menu, questions_menu, lists_menu, superadmin, start
from bot.middlewares.rate_limiter import RateLimitMiddleware, RaidProtectionMiddleware
from bot.middlewares.group_check import GroupCheckMiddleware

logger = get_logger(__name__)


async def main() -> None:
    setup_logging(settings.log_level, settings.log_file)
    logger.info("Initializing database...")
    await init_db()

    mod_engine = ModerationEngine()
    ver_engine = VerificationEngine()

    join_requests.setup_engines(mod_engine, ver_engine)
    verification.setup_engines(mod_engine, ver_engine)

    bot = Bot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    dp = Dispatcher(storage=MemoryStorage())

    # join_request-scoped middleware only — rate limiting must NOT apply to admin callbacks
    dp.chat_join_request.outer_middleware(RateLimitMiddleware(
        max_requests=settings.rate_limit_requests,
        window_seconds=settings.rate_limit_window,
    ))
    dp.chat_join_request.outer_middleware(RaidProtectionMiddleware(
        threshold=settings.raid_threshold,
        window_seconds=settings.raid_window,
    ))
    dp.chat_join_request.outer_middleware(GroupCheckMiddleware())

    # Routers — order matters: more specific handlers first
    dp.include_router(start.router)
    dp.include_router(setup.router)
    dp.include_router(superadmin.router)
    dp.include_router(settings_menu.router)
    dp.include_router(rules_menu.router)
    dp.include_router(questions_menu.router)
    dp.include_router(lists_menu.router)
    dp.include_router(join_requests.router)
    dp.include_router(verification.router)

    logger.info("Bot starting...", mode="polling")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except TelegramAPIError as e:
        logger.error("Failed to delete webhook — network issue?", error=str(e))
        return
    # Start background cleanup task for expired verification sessions
    asyncio.create_task(start_cleanup_loop(bot, interval_seconds=60))

    await dp.start_polling(
        bot,
        allowed_updates=[
            "message",
            "callback_query",
            "chat_join_request",
            "my_chat_member",
        ],
    )


if __name__ == "__main__":
    asyncio.run(main())
