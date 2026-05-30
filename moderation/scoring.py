from dataclasses import dataclass, field
from typing import List, Dict, Any
from database.models import DecisionEnum


@dataclass
class RuleMatch:
    rule_id: str
    rule_name: str
    score: float
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoringResult:
    user_id: int
    total_score: float
    matched_rules: List[RuleMatch]
    decision: DecisionEnum
    requires_verification: bool
    requires_manual_review: bool
    details: Dict[str, Any] = field(default_factory=dict)

    @property
    def matched_rule_ids(self) -> List[str]:
        return [r.rule_id for r in self.matched_rules]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "user_id": self.user_id,
            "total_score": self.total_score,
            "matched_rules": [
                {"rule_id": r.rule_id, "rule_name": r.rule_name, "score": r.score, "details": r.details}
                for r in self.matched_rules
            ],
            "decision": self.decision.value,
            "requires_verification": self.requires_verification,
            "requires_manual_review": self.requires_manual_review,
            "details": self.details,
        }
