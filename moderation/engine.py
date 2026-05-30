"""Main moderation engine — multi-tenant, queries DB per group per request."""
from typing import Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from database.models import DecisionEnum, GroupConfig, GroupWhitelist, SecurityModeEnum
from moderation import rules as rule_engine
from moderation import languages as lang_engine
from moderation import names as name_engine
from moderation.scoring import ScoringResult, RuleMatch
from logs import get_logger

logger = get_logger(__name__)


def _parse_config_value(value: str, value_type: str) -> Any:
    if value_type == "float":
        return float(value)
    if value_type == "int":
        return int(value)
    if value_type == "bool":
        return value.lower() in ("true", "1", "yes")
    return value


async def _load_group_config(db: AsyncSession, group_id: int) -> Dict[str, Any]:
    result = await db.execute(
        select(GroupConfig).where(GroupConfig.group_id == group_id)
    )
    configs = result.scalars().all()
    return {
        c.key: _parse_config_value(c.value, c.value_type)
        for c in configs
        if c.value is not None
    }


async def _is_whitelisted(user_id: int, group_id: int, db: AsyncSession) -> bool:
    result = await db.execute(
        select(GroupWhitelist).where(
            GroupWhitelist.group_id == group_id,
            GroupWhitelist.telegram_id == user_id,
        )
    )
    return result.scalar_one_or_none() is not None


class ModerationEngine:
    """Stateless engine — each evaluate() call queries the DB for group-specific config."""

    async def evaluate(
        self,
        user_data: Dict[str, Any],
        db: AsyncSession,
        group_id: int,
        verification_score: Optional[float] = None,
        raid_active: bool = False,
    ) -> ScoringResult:
        user_id = user_data.get("user_id", 0)

        if await _is_whitelisted(user_id, group_id, db):
            logger.info("User whitelisted — auto-approving", user_id=user_id, group_id=group_id)
            return ScoringResult(
                user_id=user_id,
                total_score=200.0,
                matched_rules=[RuleMatch("whitelist", "Whitelisted User", 200.0)],
                decision=DecisionEnum.approved,
                requires_verification=False,
                requires_manual_review=False,
                details={"reason": "whitelisted"},
            )

        cfg = await _load_group_config(db, group_id)

        def gcfg(key, default=None):
            return cfg.get(key, default)

        security_mode = gcfg("security_mode", "normal")
        requires_verification = gcfg("verification_required", True)
        approve_threshold = gcfg("approve_threshold", 60.0)
        reject_threshold = gcfg("reject_threshold", 0.0)
        auto_ban_threshold = gcfg("auto_ban_threshold", -100.0)
        manual_min = gcfg("manual_review_range_min", 30.0)
        manual_max = gcfg("manual_review_range_max", 60.0)
        fuzzy_threshold = gcfg("fuzzy_match_threshold", 80.0)

        if security_mode == SecurityModeEnum.lockdown:
            return ScoringResult(
                user_id=user_id,
                total_score=0.0,
                matched_rules=[],
                decision=DecisionEnum.manual_review,
                requires_verification=False,
                requires_manual_review=True,
                details={"reason": "lockdown_mode"},
            )

        matched_rules: list[RuleMatch] = []
        total_score = 0.0

        # --- Rules (per-group) ---
        rule_matches = await rule_engine.evaluate_rules(user_data, group_id, db)
        matched_rules.extend(rule_matches)
        total_score += sum(r.score for r in rule_matches)

        # --- Language detection (per-group) ---
        full_name = " ".join(filter(None, [
            user_data.get("first_name", ""),
            user_data.get("last_name", ""),
        ]))
        lang_matches = await lang_engine.detect_languages(full_name, group_id, db)
        for lang, lang_score in lang_matches:
            matched_rules.append(RuleMatch(
                rule_id=f"lang_{lang.lower()}",
                rule_name=f"Language: {lang}",
                score=lang_score,
                details={"language": lang},
            ))
            total_score += lang_score

        # --- Israeli name matching (global) ---
        name_hits = await name_engine.match_names(full_name, db, threshold=fuzzy_threshold)
        for matched_name, name_score, ratio in name_hits:
            matched_rules.append(RuleMatch(
                rule_id=f"israeli_name_{matched_name}",
                rule_name=f"Israeli Name: {matched_name}",
                score=name_score,
                details={"matched_name": matched_name, "fuzzy_ratio": ratio},
            ))
            total_score += name_score

        # --- Verification score ---
        if verification_score is not None:
            matched_rules.append(RuleMatch(
                rule_id="verification",
                rule_name="Verification Result",
                score=verification_score,
                details={"verification_score": verification_score},
            ))
            total_score += verification_score

        logger.info(
            "Moderation score calculated",
            user_id=user_id,
            group_id=group_id,
            score=total_score,
            rules_matched=len(matched_rules),
        )

        # Strict mode forces verification; raid applies a -30 score penalty
        if security_mode == SecurityModeEnum.strict:
            requires_verification = True
        if raid_active:
            total_score -= 30.0
            matched_rules.append(RuleMatch(
                rule_id="raid_penalty",
                rule_name="Raid Penalty",
                score=-30.0,
                details={"reason": "raid_active"},
            ))

        decision = DecisionEnum.pending
        requires_manual = False

        if total_score <= auto_ban_threshold:
            decision = DecisionEnum.banned
            requires_verification = False
        elif total_score >= approve_threshold and (
            not requires_verification or verification_score is not None
        ):
            decision = DecisionEnum.approved
        elif manual_min <= total_score < manual_max:
            decision = DecisionEnum.manual_review
            requires_manual = True
        elif total_score < reject_threshold:
            decision = DecisionEnum.rejected
        elif requires_verification and verification_score is None:
            decision = DecisionEnum.pending
        else:
            # score in [reject_threshold, manual_min) — not high enough for manual review
            decision = DecisionEnum.rejected

        return ScoringResult(
            user_id=user_id,
            total_score=total_score,
            matched_rules=matched_rules,
            decision=decision,
            requires_verification=requires_verification and verification_score is None,
            requires_manual_review=requires_manual,
            details={
                "security_mode": security_mode,
                "thresholds": {
                    "approve": approve_threshold,
                    "reject": reject_threshold,
                    "ban": auto_ban_threshold,
                },
            },
        )
