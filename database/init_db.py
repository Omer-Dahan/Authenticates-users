"""Initialize database tables and seed global data."""
import asyncio
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from database.session import engine, AsyncSessionLocal, Base
from database import models


DEFAULT_ISRAELI_NAMES = [
    "yossi", "moshe", "lior", "sapir", "idan", "itay", "shay", "avi",
    "matan", "eyal", "nir", "tal", "ori", "ron", "dan", "oren",
    "ofir", "yoni", "boaz", "gal", "dvir", "roi", "amit", "guy",
    "noam", "tamir", "nadav", "omri", "alon", "yuval", "gili",
    "maya", "noa", "shira", "michal", "keren", "yael", "dana",
    "efrat", "liron", "inbal", "hagit", "hila", "roni", "chen",
    "dani", "bar", "eden", "stav", "niv", "rotem",
]


async def _run_migrations() -> None:
    """Add new columns to existing tables (SQLite ALTER TABLE is idempotent here)."""
    new_columns = [
        "ALTER TABLE groups ADD COLUMN settings_last_edited_by_id INTEGER",
        "ALTER TABLE groups ADD COLUMN settings_last_edited_by_name VARCHAR(255)",
        "ALTER TABLE groups ADD COLUMN settings_last_edited_at DATETIME",
        "ALTER TABLE groups ADD COLUMN is_default_template BOOLEAN DEFAULT 0",
    ]
    async with engine.begin() as conn:
        for sql in new_columns:
            try:
                await conn.execute(text(sql))
            except OperationalError:
                pass  # column already exists


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _run_migrations()

    async with AsyncSessionLocal() as session:
        for name in DEFAULT_ISRAELI_NAMES:
            result = await session.execute(
                select(models.IsraeliNameList).where(models.IsraeliNameList.name == name)
            )
            if not result.scalar_one_or_none():
                session.add(models.IsraeliNameList(name=name, score=40.0))

        await session.commit()


# ─── Default config seeded for each new group ─────────────────────────────────

DEFAULT_GROUP_CONFIG = [
    {"key": "approve_threshold", "value": "60", "value_type": "float"},
    {"key": "reject_threshold", "value": "0", "value_type": "float"},
    {"key": "auto_ban_threshold", "value": "-100", "value_type": "float"},
    {"key": "verification_required", "value": "true", "value_type": "bool"},
    {"key": "security_mode", "value": "normal", "value_type": "string"},
    {"key": "manual_review_range_min", "value": "30", "value_type": "float"},
    {"key": "manual_review_range_max", "value": "60", "value_type": "float"},
    {"key": "fuzzy_match_threshold", "value": "80", "value_type": "float"},
    {"key": "welcome_message", "value": "ברוך הבא! אנא השלם את האימות כדי להצטרף.", "value_type": "string"},
    {"key": "approve_message", "value": "בקשתך אושרה! ברוך הבא לקבוצה.", "value_type": "string"},
    {"key": "reject_message", "value": "בקשתך לא אושרה.", "value_type": "string"},
]

DEFAULT_GROUP_LANGUAGES = [
    {"language": "Hebrew", "regex": r"[֐-׿]", "score": 70.0, "enabled": True},
    {"language": "Arabic", "regex": r"[؀-ۿ]", "score": -60.0, "enabled": True},
    {"language": "Persian", "regex": r"[؀-ۿﭐ-﷿ﹰ-﻿]", "score": 30.0, "enabled": True},
    {"language": "Russian", "regex": r"[Ѐ-ӿ]", "score": 10.0, "enabled": False},
    {"language": "Turkish", "regex": r"[ğüşıöçĞÜŞİÖÇ]", "score": 5.0, "enabled": False},
    {"language": "English", "regex": r"[a-zA-Z]", "score": -10.0, "enabled": False},
]

DEFAULT_GROUP_RULES = [
    {
        "rule_id": "hebrew_name",
        "name": "תווים עבריים בשם",
        "enabled": True,
        "rule_type": "regex",
        "target": "full_name",
        "pattern": r"[֐-׿]",
        "score": 70.0,
        "description": "מזהה תווים עבריים בשם המשתמש",
    },
    {
        "rule_id": "arabic_name",
        "name": "תווים ערביים בשם",
        "enabled": True,
        "rule_type": "regex",
        "target": "full_name",
        "pattern": r"[؀-ۿ]",
        "score": -60.0,
        "description": "מזהה תווים ערביים בשם המשתמש",
    },
    {
        "rule_id": "suspicious_username",
        "name": "שם משתמש חשוד",
        "enabled": True,
        "rule_type": "regex",
        "target": "username",
        "pattern": r"(crypto|forex|invest|trade|profit|casino|bet|earn|money|rich|free|win)",
        "score": -80.0,
        "description": "מזהה דפוסי ספאם/הונאה בשם המשתמש",
    },
    {
        "rule_id": "empty_name",
        "name": "שם קצר מאוד",
        "enabled": True,
        "rule_type": "regex",
        "target": "full_name",
        "pattern": r"^.{0,1}$",
        "score": 30.0,
        "description": "מדגיש משתמשים ללא שם או עם תו בודד",
    },
]

DEFAULT_GROUP_BLACKLIST = [
    "crypto", "forex", "casino", "onlyfans", "betting",
    "investment", "trading", "profit", "earn money", "get rich",
    "free bitcoin", "airdrop", "nft", "defi", "yield",
]

DEFAULT_GROUP_QUESTION = {
    "question": "כתוב את המילה 'שלום' בעברית כדי לאמת שאתה מבין עברית:",
    "accepted_answers": ["שלום", "shalom"],
    "validation_type": "exact_match",
    "case_sensitive": False,
    "max_attempts": 2,
    "timeout_seconds": 300,
    "ban_on_fail": False,
    "score_on_pass": 100.0,
    "score_on_fail": -50.0,
    "enabled": True,
}


async def seed_new_group(group_db_id: int, session) -> None:
    """Seed default config, rules, languages, blacklist and question for a new group."""
    for cfg in DEFAULT_GROUP_CONFIG:
        session.add(models.GroupConfig(group_id=group_db_id, **cfg))

    for lang in DEFAULT_GROUP_LANGUAGES:
        session.add(models.GroupLanguageFilter(group_id=group_db_id, **lang))

    for rule in DEFAULT_GROUP_RULES:
        session.add(models.GroupRule(group_id=group_db_id, keywords=[], **rule))

    for keyword in DEFAULT_GROUP_BLACKLIST:
        session.add(models.GroupBlacklist(group_id=group_db_id, keyword=keyword, score=-100.0))

    session.add(models.GroupQuestion(group_id=group_db_id, **DEFAULT_GROUP_QUESTION))


if __name__ == "__main__":
    asyncio.run(init_db())
