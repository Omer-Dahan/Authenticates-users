"""Background loop that quietly reconciles join_requests rows against what
Telegram can actually confirm, once a day for every group that has not opted
out. Stays silent unless a sweep actually closes something, in which case the
bot owner gets the same honest report the on-demand entry points produce.
"""
import asyncio

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from database.session import AsyncSessionLocal
from database.models import Group, GroupConfig
from moderation.actions import run_reconciliation_sweep
from config import settings
from logs import get_logger

logger = get_logger(__name__)

RECONCILE_CONFIG_KEY = "reconciliation_daily_enabled"

# Guards run_daily_reconciliation so two sweeps can never run at the same
# time in this process, whether the overlap would come from the daily loop's
# own interval, its safety-timeout fallback, or the one-off pass
# recovery/sweep.py runs right after an outage.
_sweep_lock = asyncio.Lock()

# Set (synchronously, see recovery/sweep.py) once the recovery path has made
# its post-recovery reconciliation attempt, so the daily loop's decision to
# skip its own first iteration is based on an explicit fact instead of a
# guess about how long that attempt takes.
_recovery_sweep_started = False


def mark_recovery_sweep_started() -> None:
    global _recovery_sweep_started
    _recovery_sweep_started = True


async def _group_reconcile_enabled(group_id: int, db) -> bool:
    result = await db.execute(
        select(GroupConfig).where(GroupConfig.group_id == group_id, GroupConfig.key == RECONCILE_CONFIG_KEY)
    )
    cfg = result.scalar_one_or_none()
    if not cfg or cfg.value is None:
        return True  # sane default: a group participates unless it was turned off
    return cfg.value.lower() in ("true", "1", "yes")


def _closed_count(report) -> int:
    return (
        len(report.joined) + len(report.deleted) + len(report.deleted_request_gone)
        + len(report.banned) + len(report.auto_closed_stale)
    )


async def run_daily_reconciliation(bot: Bot) -> None:
    """Runs one reconciliation pass over every opted-in group.

    Guarded by _sweep_lock so this can never overlap with another sweep
    running in this process - whether that is the daily loop's own next
    interval, its safety-timeout fallback, or the one-off pass
    recovery/sweep.py runs after an outage. A caller that finds a sweep
    already running does not start a second one, and does not pretend it ran
    either: it logs and returns, leaving the already-running sweep as the
    one source of truth for this pass.
    """
    if _sweep_lock.locked():
        logger.warning("daily reconciliation: a sweep is already running in this process; skipping this run")
        return

    async with _sweep_lock:
        from bot.handlers.ban_commands import format_reconciliation_report  # avoids a module-load cycle

        async with AsyncSessionLocal() as db:
            groups_result = await db.execute(
                select(Group).where(Group.is_active == True, Group.is_banned == False)  # noqa: E712
            )
            all_groups = groups_result.scalars().all()
            group_ids = [g.id for g in all_groups if await _group_reconcile_enabled(g.id, db)]

        for group_id in group_ids:
            try:
                async with AsyncSessionLocal() as db:
                    report = await run_reconciliation_sweep(bot, group_id=group_id, dry_run=False, db=db)
            except Exception:
                logger.exception("daily reconciliation: sweep failed", group_id=group_id)
                continue

            # Stay silent only when the sweep both closed nothing AND has
            # nothing to disclose - a group stuck behind a permanently
            # failing probe must still reach the owner even if it closed
            # zero rows this run, or it can hide behind this check forever.
            if _closed_count(report) == 0 and not report.stale_skipped_groups:
                continue

            try:
                await bot.send_message(
                    settings.super_admin_id,
                    f"🔁 <b>בדיקת התאמה יומית סגרה שורות</b>\n\n{format_reconciliation_report(report, applied=True)}",
                    parse_mode="HTML",
                )
            except TelegramAPIError:
                logger.warning("daily reconciliation: could not notify the owner", group_id=group_id)


async def _run_guarded(bot: Bot) -> None:
    try:
        await run_daily_reconciliation(bot)
    except Exception as e:  # pylint: disable=broad-except
        logger.error("Error in daily reconciliation loop", error=str(e))


async def start_daily_reconciliation_loop(
    bot: Bot,
    interval_seconds: int = None,
    recovery_done_event: "asyncio.Event | None" = None,
    safety_timeout_seconds: int = 120,
) -> None:
    """Runs the daily quiet sweep forever, honouring the global on/off switch
    and the configured interval.

    If recovery_done_event is given, the first run waits for the outage
    recovery sweep to finish first - same rule as start_cleanup_loop in
    verification/cleanup.py - so this loop's own first pass cannot race the
    one recovery/sweep.py (run_recovery_sweep) already runs right after
    setting that event. Whether that pass already covers the outage is
    checked explicitly via _recovery_sweep_started (set by recovery/sweep.py
    itself), not guessed from a sleep: if it has, this loop's own first run
    is skipped once and deferred to the next normal interval; if it has not
    (including when the wait above timed out, so recovery may still be
    stuck), this loop runs its own first pass right away as a fallback.
    Either way, run_daily_reconciliation's own lock makes actual overlap
    with recovery's pass impossible regardless of how these two race.
    Bounded by safety_timeout_seconds so a stuck sweep can never block this
    loop forever.
    """
    if not settings.reconciliation_daily_sweep_enabled:
        logger.info("Daily reconciliation loop disabled via settings")
        return
    if interval_seconds is None:
        interval_seconds = settings.reconciliation_daily_interval_seconds

    logger.info("Starting daily reconciliation loop...", interval_seconds=interval_seconds)
    if recovery_done_event is not None:
        try:
            await asyncio.wait_for(recovery_done_event.wait(), timeout=safety_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("Recovery sweep did not finish before safety timeout; starting reconciliation anyway")
        if _recovery_sweep_started:
            await asyncio.sleep(interval_seconds)
        else:
            await _run_guarded(bot)
            await asyncio.sleep(interval_seconds)
    while True:
        await _run_guarded(bot)
        await asyncio.sleep(interval_seconds)
