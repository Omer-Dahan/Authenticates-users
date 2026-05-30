"""Global Israeli names fuzzy matching (shared across all groups)."""
import asyncio
from typing import List, Tuple
from rapidfuzz import fuzz, process
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import IsraeliNameList
from logs import get_logger

logger = get_logger(__name__)

# Module-level cache — Israeli names are global and rarely change.
_names_cache: List[Tuple[str, float]] = []
_cache_loaded = False
_load_lock = asyncio.Lock()


async def load_names(db: AsyncSession) -> None:
    global _names_cache, _cache_loaded  # pylint: disable=global-statement
    result = await db.execute(
        select(IsraeliNameList).where(IsraeliNameList.enabled)
    )
    names = result.scalars().all()
    _names_cache = [(n.name.lower(), n.score) for n in names]
    _cache_loaded = True
    logger.info("Loaded Israeli names", count=len(_names_cache))


async def match_names(
    text: str,
    db: AsyncSession,
    threshold: float = 80.0,
) -> List[Tuple[str, float, float]]:
    """Return list of (matched_name, score, fuzzy_ratio) for all matches in text."""
    if not _cache_loaded:
        async with _load_lock:
            if not _cache_loaded:
                await load_names(db)

    if not text or not _names_cache:
        return []

    name_list = [n for n, _ in _names_cache]
    name_score_map = dict(_names_cache)
    seen_words: set = set()
    matches = []

    for word in text.lower().split():
        if len(word) < 2 or word in seen_words:
            continue
        seen_words.add(word)

        result = process.extractOne(word, name_list, scorer=fuzz.ratio, score_cutoff=threshold)
        if result:
            matched_name, ratio, _ = result
            matches.append((matched_name, name_score_map.get(matched_name, 40.0), ratio))

    return matches
