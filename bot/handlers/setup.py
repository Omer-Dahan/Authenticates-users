"""Group registration flow: bot-added event + /setup command."""
from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, ChatMemberUpdated

from database.session import AsyncSessionLocal
from database.models import Group
from database.init_db import seed_new_group
from database.clone_utils import clone_group_settings
from bot.handlers.admin_utils import register_group_admin
from sqlalchemy import select
from logs import get_logger

logger = get_logger(__name__)
router = Router()


async def _is_chat_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("administrator", "creator")
    except Exception:
        return False


async def _get_or_none(chat_id: int, db) -> Group | None:
    result = await db.execute(select(Group).where(Group.chat_id == chat_id))
    return result.scalar_one_or_none()


# ─── Bot added to group ───────────────────────────────────────────────────────

@router.my_chat_member(F.new_chat_member.status.in_({"member", "administrator"}))
async def bot_added_to_group(event: ChatMemberUpdated, bot: Bot) -> None:
    if event.chat.type not in ("group", "supergroup"):
        return

    chat_id = event.chat.id
    adder_id = event.from_user.id
    logger.info("Bot added to group", chat_id=chat_id, adder_id=adder_id, chat_title=event.chat.title)

    async with AsyncSessionLocal() as db:
        existing = await _get_or_none(chat_id, db)

        if existing:
            logger.info("Group already registered, skipping", chat_id=chat_id)
            group_db_id = existing.id
        else:
            group = Group(
                chat_id=chat_id,
                title=event.chat.title,
                username=getattr(event.chat, "username", None),
                owner_id=adder_id,
                is_active=True,
                is_banned=False,
            )
            db.add(group)
            await db.flush()
            group_db_id = group.id

            default_group = await db.scalar(
                select(Group).where(Group.owner_id == adder_id, Group.is_default_template == True)
            )
            if default_group:
                await clone_group_settings(default_group.id, group_db_id, db)
            else:
                await seed_new_group(group_db_id, db)

        # Register all current Telegram admins so they can see the group in their dashboard
        try:
            tg_admins = await bot.get_chat_administrators(chat_id)
            for member in tg_admins:
                if not member.user.is_bot:
                    await register_group_admin(
                        group_db_id,
                        member.user.id,
                        member.user.username,
                        member.user.first_name,
                        db,
                    )
        except Exception as e:
            logger.warning("Could not fetch admins for GroupAdmin registration", chat_id=chat_id, error=str(e))

        await db.commit()
        if not existing:
            logger.info("Group auto-registered", chat_id=chat_id, owner_id=adder_id)

    try:
        me = await bot.get_me()
        bot_username = me.username
        await bot.send_message(
            chat_id,
            f"👋 <b>שלום! הבוט נוסף לקבוצה ונרשם בהצלחה.</b>\n\n"
            f"לניהול ההגדרות:\n"
            f"👉 <a href=\"https://t.me/{bot_username}\">פתח הגדרות בפרטי</a>\n\n"
            f"<i>כל הניהול מתבצע בצ'אט הפרטי עם הבוט.</i>",
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning("Could not send bot-added message", chat_id=chat_id, error=str(e))


# ─── /setup ───────────────────────────────────────────────────────────────────

@router.message(Command("setup"))
async def cmd_setup(message: Message, bot: Bot) -> None:
    if message.chat.type not in ("group", "supergroup"):
        await message.reply("❌ /setup יש להפעיל בתוך קבוצה.")
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    if not await _is_chat_admin(bot, chat_id, user_id):
        await message.reply("❌ רק מנהלי הקבוצה יכולים להפעיל /setup.")
        return

    async with AsyncSessionLocal() as db:
        existing = await _get_or_none(chat_id, db)

        if existing:
            # Register this admin in case they weren't there yet
            await register_group_admin(existing.id, user_id, message.from_user.username, message.from_user.first_name, db)
            await db.commit()

            status = "🟢 פעיל" if existing.is_active else "🔴 מושהה"
            me = await bot.get_me()
            bot_username = me.username
            await message.reply(
                f"✅ הקבוצה כבר רשומה במערכת.\n"
                f"סטטוס: {status}\n\n"
                f"👉 <a href=\"https://t.me/{bot_username}\">ניהול הגדרות בפרטי</a>",
                parse_mode="HTML",
            )
            return

        chat = message.chat
        group = Group(
            chat_id=chat_id,
            title=chat.title,
            username=chat.username,
            owner_id=user_id,
            is_active=True,
            is_banned=False,
        )
        db.add(group)
        await db.flush()

        default_group = await db.scalar(
            select(Group).where(Group.owner_id == user_id, Group.is_default_template == True)
        )
        if default_group:
            await clone_group_settings(default_group.id, group.id, db)
        else:
            await seed_new_group(group.id, db)

        # Register all current admins
        try:
            tg_admins = await bot.get_chat_administrators(chat_id)
            for member in tg_admins:
                if not member.user.is_bot:
                    await register_group_admin(group.id, member.user.id, member.user.username, member.user.first_name, db)
        except Exception as e:
            logger.warning("Could not fetch admins for GroupAdmin registration", chat_id=chat_id, error=str(e))

        await db.commit()

        logger.info("Group registered", chat_id=chat_id, owner_id=user_id)

    me = await bot.get_me()
    bot_username = me.username
    await message.reply(
        "✅ <b>הקבוצה נרשמה בהצלחה!</b>\n\n"
        "הגדרות ברירת מחדל הותקנו:\n"
        "• כללי סינון סטנדרטיים\n"
        "• שאלת אימות בסיסית\n"
        "• מצב אבטחה: רגיל\n\n"
        f"👉 <a href=\"https://t.me/{bot_username}\">ניהול הגדרות בפרטי</a>",
        parse_mode="HTML",
    )
