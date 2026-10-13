#!/usr/bin/env python
"""Terminal CLI that prints, per managed group, the real pending-join-request
count read straight from Telegram next to the bot's own open-row count.

Runnable from the project root on the host that runs the bot, without the
bot process itself running:

    venv/bin/python scripts/pending_counts.py

Read-only: it only issues GetFullChannelRequest calls and a local DB read,
it never approves, declines, or writes anything.
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select, func  # noqa: E402

from database.init_db import init_db  # noqa: E402
from database.session import AsyncSessionLocal  # noqa: E402
from database.models import Group, JoinRequest, DecisionEnum  # noqa: E402
from telegram_mtproto.pending_counts import fetch_pending_counts, mtproto_available  # noqa: E402


async def _run() -> int:
    if not mtproto_available():
        print(
            "❌ התכונה כבויה: חסר telethon, חסרים TELEGRAM_API_ID/TELEGRAM_API_HASH, "
            "או TELEGRAM_PENDING_ENABLED=false."
        )
        return 1

    await init_db()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Group).where(Group.is_active == True, Group.is_banned == False)  # noqa: E712
        )
        groups = result.scalars().all()

        open_rows = {}
        for group in groups:
            open_rows[group.id] = await db.scalar(
                select(func.count(JoinRequest.id)).where(
                    JoinRequest.group_id == group.id,
                    JoinRequest.decision == DecisionEnum.pending,
                )
            ) or 0

    if not groups:
        print("אין קבוצות פעילות.")
        return 0

    real_counts = await fetch_pending_counts([group.chat_id for group in groups])

    for group in groups:
        real = real_counts.get(group.chat_id)
        real_text = str(real) if real is not None else "לא ידוע"
        title = group.title or str(group.chat_id)
        print(f"{title}: טלגרם={real_text} | אצלנו={open_rows[group.id]}")

    return 0


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
