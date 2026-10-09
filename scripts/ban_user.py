#!/usr/bin/env python
"""Terminal CLI for the manual-ban feature (access path 4).

Runnable from the project root on the host that runs the bot:

    venv/bin/python scripts/ban_user.py <telegram_id|@username> --scope global|global_remove|local \
        [--group <chat_id>] [--reason "..."] [--actor <telegram_id>] [--unban] [--dry-run]
"""
import argparse
import asyncio
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiogram import Bot  # noqa: E402
from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from config import settings  # noqa: E402
from database.init_db import init_db  # noqa: E402
from database.session import AsyncSessionLocal  # noqa: E402
from database.models import Group, TelegramUser  # noqa: E402
from moderation.actions import ban_user, unban_user, BanReport  # noqa: E402


def format_report(report: BanReport) -> str:
    lines = []
    if report.banned_in:
        lines.append("🔨 נחסם בקבוצות: " + ", ".join(report.banned_in))
    if report.removed_from:
        lines.append("🚪 הוסר מהקבוצות: " + ", ".join(report.removed_from))
    if report.failed:
        for title, err in report.failed:
            lines.append(f"⚠️ כשל ב-{title}: {err}")
    if report.skipped:
        lines.append("⏭ דולג: " + ", ".join(report.skipped))
    return "\n".join(lines) if lines else "לא בוצעה פעולה באף קבוצה."


async def _resolve_target(identifier: str, db) -> Optional[int]:
    identifier = identifier.strip()
    if identifier.startswith("@"):
        result = await db.execute(select(TelegramUser).where(TelegramUser.username == identifier[1:]))
        user = result.scalar_one_or_none()
        return user.telegram_id if user else None
    try:
        return int(identifier)
    except ValueError:
        return None


async def _run(args: argparse.Namespace) -> int:
    bot = Bot(token=settings.bot_token)
    try:
        try:
            await init_db()
            async with AsyncSessionLocal() as db:
                telegram_id = await _resolve_target(args.identifier, db)
                if telegram_id is None:
                    print(f"❌ לא נמצא משתמש עבור {args.identifier}.")
                    return 1

                group_id = None
                if args.group is not None:
                    result = await db.execute(select(Group).where(Group.chat_id == args.group))
                    group = result.scalar_one_or_none()
                    if not group:
                        print(f"❌ לא נמצאה קבוצה עם chat_id {args.group}.")
                        return 1
                    group_id = group.id

                if args.unban:
                    report = await unban_user(
                        bot, telegram_id,
                        group_id=group_id,
                        actor_id=args.actor,
                        actor_name="cli",
                        db=db,
                        dry_run=args.dry_run,
                    )
                    print(f"✅ החסימה הוסרה (telegram_id={telegram_id}).")
                else:
                    if args.scope is None:
                        print("❌ יש לציין --scope בעת חסימה.")
                        return 1
                    try:
                        report = await ban_user(
                            bot, telegram_id, args.scope,
                            group_id=group_id,
                            reason=args.reason,
                            actor_id=args.actor,
                            actor_name="cli",
                            db=db,
                            dry_run=args.dry_run,
                        )
                    except ValueError as e:
                        print(f"❌ {e}")
                        return 1
                    print(f"🔨 המשתמש נחסם (telegram_id={telegram_id}, scope={args.scope}).")

                print(format_report(report))
                return 0
        except SQLAlchemyError as e:
            print(f"❌ שגיאה בגישה למסד הנתונים: {e}")
            return 1
    finally:
        await bot.session.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manual user-ban CLI for auth-bot")
    parser.add_argument("identifier", help="telegram_id or @username")
    parser.add_argument("--scope", choices=["global", "global_remove", "local"], default=None)
    parser.add_argument(
        "--group", type=int, default=None,
        help="origin group chat_id, required for --scope local/global_remove",
    )
    parser.add_argument("--reason", default=None)
    parser.add_argument("--actor", type=int, default=None, help="telegram_id of the admin issuing this action")
    parser.add_argument("--unban", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    exit_code = asyncio.run(_run(args))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
