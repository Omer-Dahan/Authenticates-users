"""Per-group language detection."""
import re
from typing import List, Tuple
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import GroupLanguageFilter
from logs import get_logger

logger = get_logger(__name__)


async def detect_languages(
    text: str,
    group_id: int,
    db: AsyncSession,
) -> List[Tuple[str, float]]:
    """Return list of (language_name, score) for all enabled languages detected in text."""
    if not text:
        return []

    result = await db.execute(
        select(GroupLanguageFilter).where(
            GroupLanguageFilter.group_id == group_id,
            GroupLanguageFilter.enabled,
        )
    )
    filters = result.scalars().all()

    detected = []
    for lang in filters:
        try:
            if re.compile(lang.regex, re.UNICODE).search(text):
                detected.append((lang.language, lang.score))
        except re.error as e:
            logger.error("Invalid regex in language filter", language=lang.language, error=str(e))

    return detected
