#!/usr/bin/env python
"""Terminal CLI for purging pending join requests from deleted Telegram accounts.

Runnable from the project root on the host that runs the bot:

    venv/bin/python scripts/purge_deleted.py [--group <chat_id>] [--apply] [--actor <telegram_id>]

Without --apply this is a dry run: it reports what it found and changes nothing.
"""
import argparse
import html
import re
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from config import settings  # noqa: E402
from database.init_db import init_db  # noqa: E402
from database.session import AsyncSessionLocal  # noqa: E402
from database.models import Group  # noqa: E402
from moderation.actions import purge_deleted_requests, PurgeReport  # noqa: E402
from bot.handlers.ban_commands import format_purge_report  # noqa: E402


def format_report(report: PurgeReport, applied: bool) -> str:
    # Same wording and buckets as the bot's report, as plain terminal text.
    text = re.sub(r"</?(b|code)>", "", format_purge_report(report, applied))
    return html.unescape(text)


async def _run(args: argparse.Namespace) -> int:
    bot = Bot(token=settings.bot_token)
    try:
        try:
            await init_db()
            async with AsyncSessionLocal() as db:
                group_id = None
                if args.group is not None:
                    result = await db.execute(select(Group).where(Group.chat_id == args.group))
                    group = result.scalar_one_or_none()
                    if not group:
                        print(f"❌ לא נמצאה קבוצה עם chat_id {args.group}.")
                        return 1
                    group_id = group.id

                report = await purge_deleted_requests(
                    bot, group_id=group_id, dry_run=not args.apply,
                    actor_id=args.actor, actor_name="cli", db=db,
                )
                print(format_report(report, applied=args.apply))
                return 0
        except SQLAlchemyError as e:
            print(f"❌ שגיאה בגישה למסד הנתונים: {e}")
            return 1
    finally:
        await bot.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Purge pending join requests belonging to deleted Telegram accounts"
    )
    parser.add_argument(
        "--group", type=int, default=None,
        help="origin group chat_id; omit to scan every active group",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="actually decline and close the rows (default: dry run)",
    )
    parser.add_argument("--actor", type=int, default=None, help="telegram_id of the admin issuing this action")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
