"""Utility to clone settings from one group to another."""
from sqlalchemy import select, delete, func
from database.models import (
    GroupConfig, GroupRule, GroupQuestion, GroupLanguageFilter, GroupBlacklist
)

_MODELS = [GroupConfig, GroupRule, GroupQuestion, GroupLanguageFilter, GroupBlacklist]


async def are_settings_different(source_group_id: int, target_group_id: int, session) -> bool:
    """Return True if row counts differ between source and target across all setting tables."""
    for model in _MODELS:
        sc = await session.scalar(select(func.count(model.id)).where(model.group_id == source_group_id))
        tc = await session.scalar(select(func.count(model.id)).where(model.group_id == target_group_id))
        if sc != tc:
            return True
    return True

async def get_difference_summary(source_group_id: int, target_group_id: int, session) -> str:
    models_labels = [
        (GroupConfig, "הגדרות"),
        (GroupRule, "כללים"),
        (GroupQuestion, "שאלות"),
        (GroupLanguageFilter, "שפות"),
        (GroupBlacklist, "מילים חסומות")
    ]
    
    summary_parts = []
    has_diff = False
    for model, label in models_labels:
        sc = await session.scalar(select(func.count(model.id)).where(model.group_id == source_group_id))
        tc = await session.scalar(select(func.count(model.id)).where(model.group_id == target_group_id))
        if sc != tc:
            has_diff = True
        summary_parts.append(f"• {label}: {tc} ⬅️ במקום ➡️ {sc}")
        
    if not has_diff:
        return "לא נמצאו הבדלים בכמות ההגדרות, אך ייתכן שערכים פנימיים שונו."
        
    return "שינויים צפויים:\n" + "\n".join(summary_parts)


async def clone_group_settings(source_group_id: int, target_group_id: int, session) -> None:
    """Clone all settings from source_group to target_group."""
    # 1. Delete existing settings in target
    for model in [GroupConfig, GroupRule, GroupQuestion, GroupLanguageFilter, GroupBlacklist]:
        await session.execute(delete(model).where(model.group_id == target_group_id))
        
    await session.flush()
    
    # 2. Copy from source
    # GroupConfig
    result = await session.execute(select(GroupConfig).where(GroupConfig.group_id == source_group_id))
    for cfg in result.scalars().all():
        session.add(GroupConfig(group_id=target_group_id, key=cfg.key, value=cfg.value, value_type=cfg.value_type))
        
    # GroupRule
    result = await session.execute(select(GroupRule).where(GroupRule.group_id == source_group_id))
    for rule in result.scalars().all():
        session.add(GroupRule(
            group_id=target_group_id, rule_id=rule.rule_id, name=rule.name, enabled=rule.enabled,
            rule_type=rule.rule_type, target=rule.target, pattern=rule.pattern, keywords=rule.keywords,
            score=rule.score, description=rule.description
        ))

    # GroupQuestion
    result = await session.execute(select(GroupQuestion).where(GroupQuestion.group_id == source_group_id))
    for q in result.scalars().all():
        session.add(GroupQuestion(
            group_id=target_group_id, name=q.name, question=q.question, accepted_answers=q.accepted_answers,
            validation_type=q.validation_type, case_sensitive=q.case_sensitive, max_attempts=q.max_attempts,
            timeout_seconds=q.timeout_seconds, ban_on_fail=q.ban_on_fail, score_on_pass=q.score_on_pass,
            score_on_fail=q.score_on_fail, enabled=q.enabled
        ))

    # GroupLanguageFilter
    result = await session.execute(select(GroupLanguageFilter).where(GroupLanguageFilter.group_id == source_group_id))
    for lang in result.scalars().all():
        session.add(GroupLanguageFilter(
            group_id=target_group_id, language=lang.language, regex=lang.regex, score=lang.score, enabled=lang.enabled
        ))

    # GroupBlacklist
    result = await session.execute(select(GroupBlacklist).where(GroupBlacklist.group_id == source_group_id))
    for bl in result.scalars().all():
        session.add(GroupBlacklist(
            group_id=target_group_id, keyword=bl.keyword, score=bl.score, enabled=bl.enabled
        ))
        
    await session.flush()
