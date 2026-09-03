import pytest
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession

from aiogram import Bot
from aiogram.types import User, Chat, ChatJoinRequest, CallbackQuery, Message

from database.session import Base
import database.session
import bot.handlers.join_requests
import bot.handlers.settings_menu
import bot.handlers.verification
from database.models import Group, GroupConfig
from moderation import ModerationEngine
from verification import VerificationEngine
from config import settings


@pytest.fixture
async def test_session_factory(tmp_path, monkeypatch):
    db_file = tmp_path / "test_moderation.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    
    test_engine = create_async_engine(
        db_url,
        echo=False,
        connect_args={"check_same_thread": False},
    )
    
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    session_factory = async_sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    
    # Patch sessionmaker across all relevant modules
    monkeypatch.setattr(database.session, "engine", test_engine)
    monkeypatch.setattr(database.session, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(bot.handlers.join_requests, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(bot.handlers.settings_menu, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(bot.handlers.verification, "AsyncSessionLocal", session_factory)
    monkeypatch.setattr(settings, "tracking_channel_id", None)
    
    yield session_factory
    
    await test_engine.dispose()


@pytest.fixture
async def db_session(test_session_factory):
    async with test_session_factory() as session:
        yield session


@pytest.fixture
def mock_bot():
    bot = AsyncMock(spec=Bot)
    bot.send_message = AsyncMock()
    bot.approve_chat_join_request = AsyncMock()
    bot.decline_chat_join_request = AsyncMock()
    bot.ban_chat_member = AsyncMock()
    return bot


@pytest.fixture
def setup_bot_engines():
    mod_engine = ModerationEngine()
    ver_engine = VerificationEngine()
    bot.handlers.join_requests.setup_engines(mod_engine, ver_engine)
    bot.handlers.verification.setup_engines(mod_engine, ver_engine)
    return mod_engine, ver_engine


@pytest.fixture
async def sample_group(db_session):
    group = Group(
        chat_id=-1001234567890,
        title="Production Community",
        username="prod_comm",
        owner_id=987654321,
        is_active=True,
    )
    db_session.add(group)
    await db_session.commit()
    await db_session.refresh(group)
    return group


def make_tg_user(user_id=111222, first_name="Test", last_name="User", username="testuser"):
    return User(
        id=user_id,
        is_bot=False,
        first_name=first_name,
        last_name=last_name,
        username=username,
    )


def make_tg_chat(chat_id=-1001234567890, title="Production Community"):
    return Chat(
        id=chat_id,
        type="supergroup",
        title=title,
    )


def make_join_request(user=None, chat=None):
    if user is None:
        user = make_tg_user()
    if chat is None:
        chat = make_tg_chat()
    return ChatJoinRequest(
        chat=chat,
        from_user=user,
        user_chat_id=user.id,
        date=datetime.now(timezone.utc),
    )


def make_callback_query(data: str, user=None, chat=None):
    if user is None:
        user = make_tg_user(user_id=987654321, first_name="Admin", username="adminuser")
    if chat is None:
        chat = make_tg_chat()
    msg = AsyncMock(spec=Message)
    msg.message_id = 42
    msg.date = datetime.now(timezone.utc)
    msg.chat = chat
    msg.text = "Settings menu"
    msg.edit_text = AsyncMock()

    cb = AsyncMock(spec=CallbackQuery)
    cb.data = data
    cb.from_user = user
    cb.message = msg
    cb.answer = AsyncMock()
    return cb
