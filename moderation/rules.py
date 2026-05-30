"""Per-group rule evaluation engine."""
import re
import concurrent.futures
from typing import List, Optional, Dict, Any
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from database.models import GroupRule, GroupBlacklist, RuleTypeEnum, RuleTargetEnum
from moderation.scoring import RuleMatch
from logs import get_logger

logger = get_logger(__name__)

_regex_executor = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="regex")


def _safe_regex_search(pattern: str, text: str, flags: int, timeout: float = 0.5) -> bool:
    future = _regex_executor.submit(re.compile(pattern, flags).search, text)
    try:
        return bool(future.result(timeout=timeout))
    except concurrent.futures.TimeoutError:
        logger.warning("Regex timeout — rule skipped", pattern=pattern[:80])
        return False


def _get_target_value(user_data: Dict[str, Any], target: str) -> str:
    if target == RuleTargetEnum.full_name:
        parts = [
            user_data.get("first_name", "") or "",
            user_data.get("last_name", "") or "",
        ]
        return " ".join(p for p in parts if p).strip()
    return (user_data.get(target, "") or "").strip()


def _evaluate_rule(rule: GroupRule, user_data: Dict[str, Any]) -> Optional[RuleMatch]:
    target_value = _get_target_value(user_data, rule.target)

    if not target_value and rule.rule_type not in (RuleTypeEnum.blacklist, RuleTypeEnum.whitelist):
        return None

    matched = False
    details: Dict[str, Any] = {"target": rule.target, "value": target_value}

    try:
        if rule.rule_type == RuleTypeEnum.regex and rule.pattern:
            if _safe_regex_search(rule.pattern, target_value, re.UNICODE | re.IGNORECASE):
                matched = True
                details["pattern"] = rule.pattern

        elif rule.rule_type == RuleTypeEnum.exact_match and rule.pattern:
            matched = target_value.lower() == rule.pattern.lower()
            details["pattern"] = rule.pattern

        elif rule.rule_type in (RuleTypeEnum.keyword, RuleTypeEnum.blacklist, RuleTypeEnum.whitelist):
            for kw in (rule.keywords or []):
                if kw.lower() in target_value.lower():
                    matched = True
                    details["keyword"] = kw
                    break

    except re.error as e:
        logger.error("Invalid regex in rule", rule_id=rule.rule_id, error=str(e))
        return None

    if matched:
        return RuleMatch(rule_id=rule.rule_id, rule_name=rule.name, score=rule.score, details=details)
    return None


async def evaluate_rules(
    user_data: Dict[str, Any],
    group_id: int,
    db: AsyncSession,
) -> List[RuleMatch]:
    """Load enabled rules for group and evaluate against user_data."""
    rules_result = await db.execute(
        select(GroupRule).where(GroupRule.group_id == group_id, GroupRule.enabled)
    )
    rules = rules_result.scalars().all()

    blacklist_result = await db.execute(
        select(GroupBlacklist).where(GroupBlacklist.group_id == group_id, GroupBlacklist.enabled)
    )
    blacklist = blacklist_result.scalars().all()

    matches: List[RuleMatch] = []

    for rule in rules:
        match = _evaluate_rule(rule, user_data)
        if match:
            matches.append(match)

    full_text = " ".join(filter(None, [
        user_data.get("first_name", ""),
        user_data.get("last_name", ""),
        user_data.get("username", ""),
    ])).lower()

    for entry in blacklist:
        if entry.keyword.lower() in full_text:
            matches.append(RuleMatch(
                rule_id=f"blacklist_{entry.keyword}",
                rule_name=f"Blacklist: {entry.keyword}",
                score=entry.score,
                details={"keyword": entry.keyword},
            ))

    return matches
