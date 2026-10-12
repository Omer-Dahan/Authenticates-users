from sqlalchemy import (
    Column, Integer, String, Boolean, Float, DateTime, Text, JSON,
    ForeignKey, Enum as SAEnum, BigInteger, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
from database.session import Base
import enum


class DecisionEnum(str, enum.Enum):
    approved = "approved"
    rejected = "rejected"
    pending = "pending"
    banned = "banned"
    manual_review = "manual_review"
    # A reconciliation sweep could not prove any outcome for this row and it
    # aged past the stale window, so it was closed honestly as "we could not
    # tell" instead of being left open forever or misreported as approved or
    # rejected. See moderation/actions.py (run_reconciliation_sweep).
    stale = "stale"
    # A deleted-account join request whose decline was refused by Telegram
    # (an explicit "user is deactivated" restriction) and was cleared by
    # banning the account directly instead. Distinct from "rejected" (a
    # clean decline) and from "banned" (a rule-violation or manual ban) so
    # every report and the settings stats can show this outcome as its own
    # honest line instead of folding it into either bucket. See
    # moderation/actions.py (clear_deleted_account_request).
    cleared_via_ban = "cleared_via_ban"


class SecurityModeEnum(str, enum.Enum):
    normal = "normal"
    strict = "strict"
    lockdown = "lockdown"


class RuleTypeEnum(str, enum.Enum):
    regex = "regex"
    keyword = "keyword"
    blacklist = "blacklist"
    whitelist = "whitelist"
    exact_match = "exact_match"


class RuleTargetEnum(str, enum.Enum):
    first_name = "first_name"
    last_name = "last_name"
    username = "username"
    full_name = "full_name"
    verification_answer = "verification_answer"


# ─── Multi-tenant core ────────────────────────────────────────────────────────

class Group(Base):
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    chat_id = Column(BigInteger, unique=True, nullable=False, index=True)
    title = Column(String(255), nullable=True)
    username = Column(String(255), nullable=True)
    owner_id = Column(BigInteger, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    is_banned = Column(Boolean, default=False, nullable=False)
    is_default_template = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    settings_last_edited_by_id = Column(BigInteger, nullable=True)
    settings_last_edited_by_name = Column(String(255), nullable=True)
    settings_last_edited_at = Column(DateTime(timezone=True), nullable=True)

    config = relationship("GroupConfig", back_populates="group", cascade="all, delete-orphan")
    rules = relationship("GroupRule", back_populates="group", cascade="all, delete-orphan")
    questions = relationship("GroupQuestion", back_populates="group", cascade="all, delete-orphan")
    blacklist = relationship("GroupBlacklist", back_populates="group", cascade="all, delete-orphan")
    whitelist = relationship("GroupWhitelist", back_populates="group", cascade="all, delete-orphan")
    language_filters = relationship("GroupLanguageFilter", back_populates="group", cascade="all, delete-orphan")
    join_requests = relationship("JoinRequest", back_populates="group")
    logs = relationship("ModerationLog", back_populates="group")
    admins = relationship("GroupAdmin", back_populates="group", cascade="all, delete-orphan")


class GroupConfig(Base):
    """Per-group key-value configuration (thresholds, messages, security_mode)."""
    __tablename__ = "group_config"
    __table_args__ = (UniqueConstraint("group_id", "key"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(255), nullable=False)
    value = Column(Text, nullable=True)
    value_type = Column(String(50), default="string")

    group = relationship("Group", back_populates="config")


class GroupRule(Base):
    """Per-group moderation rules."""
    __tablename__ = "group_rules"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    rule_id = Column(String(100), nullable=False)
    name = Column(String(255), nullable=False)
    enabled = Column(Boolean, default=True)
    rule_type = Column(SAEnum(RuleTypeEnum), nullable=False)
    target = Column(SAEnum(RuleTargetEnum), nullable=False)
    pattern = Column(Text, nullable=True)
    keywords = Column(JSON, default=list)
    score = Column(Float, nullable=False, default=0.0)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", back_populates="rules")


class GroupQuestion(Base):
    """Per-group verification questions."""
    __tablename__ = "group_questions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=True)
    question = Column(Text, nullable=False)
    accepted_answers = Column(JSON, nullable=False, default=list)
    validation_type = Column(String(50), default="exact_match")
    case_sensitive = Column(Boolean, default=False)
    max_attempts = Column(Integer, default=3)
    timeout_seconds = Column(Integer, default=86400)
    ban_on_fail = Column(Boolean, default=False)
    score_on_pass = Column(Float, default=100.0)
    score_on_fail = Column(Float, default=-100.0)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", back_populates="questions")
    sessions = relationship("VerificationSession", back_populates="question")


class GroupBlacklist(Base):
    """Per-group blacklist keywords."""
    __tablename__ = "group_blacklist"
    __table_args__ = (UniqueConstraint("group_id", "keyword"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword = Column(String(255), nullable=False)
    score = Column(Float, default=-100.0)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", back_populates="blacklist")


class GroupWhitelist(Base):
    """Per-group whitelisted users (always auto-approved)."""
    __tablename__ = "group_whitelist"
    __table_args__ = (UniqueConstraint("group_id", "telegram_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=False)
    username = Column(String(255), nullable=True)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", back_populates="whitelist")


class GroupLanguageFilter(Base):
    """Per-group language detection rules."""
    __tablename__ = "group_language_filters"
    __table_args__ = (UniqueConstraint("group_id", "language"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    language = Column(String(100), nullable=False)
    regex = Column(Text, nullable=False)
    score = Column(Float, nullable=False, default=0.0)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", back_populates="language_filters")


# ─── Global tables (shared across all groups) ─────────────────────────────────

class IsraeliNameList(Base):
    """Global list of Israeli names used for fuzzy name matching."""
    __tablename__ = "israeli_names"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(255), unique=True, nullable=False)
    score = Column(Float, default=40.0)
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


# ─── Users & sessions ─────────────────────────────────────────────────────────

class TelegramUser(Base):
    __tablename__ = "telegram_users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, unique=True, nullable=False)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    language_code = Column(String(10), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    join_requests = relationship("JoinRequest", back_populates="user")
    verification_sessions = relationship("VerificationSession", back_populates="user")


class VerificationSession(Base):
    __tablename__ = "verification_sessions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, ForeignKey("telegram_users.telegram_id"), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    question_id = Column(Integer, ForeignKey("group_questions.id"), nullable=True)
    attempts = Column(Integer, default=0)
    passed = Column(Boolean, nullable=True)
    answer_given = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    # Set only once the verification question DM was actually delivered - the
    # source of truth outage recovery uses to tell "never got a message" apart
    # from "got it, then the bot went down before they answered".
    notified_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("TelegramUser", back_populates="verification_sessions")
    question = relationship("GroupQuestion", back_populates="sessions")


# ─── Event logs ───────────────────────────────────────────────────────────────

class JoinRequest(Base):
    __tablename__ = "join_requests"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True, index=True)
    user_id = Column(BigInteger, ForeignKey("telegram_users.telegram_id"), nullable=False)
    chat_id = Column(BigInteger, nullable=False)
    decision = Column(SAEnum(DecisionEnum), default=DecisionEnum.pending, nullable=False)
    score = Column(Float, default=0.0)
    matched_rules = Column(JSON, default=list)
    notes = Column(Text, nullable=True)
    reviewed_by = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)

    group = relationship("Group", back_populates="join_requests")
    user = relationship("TelegramUser", back_populates="join_requests")
    logs = relationship("ModerationLog", back_populates="join_request")


class ModerationLog(Base):
    __tablename__ = "moderation_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True, index=True)
    join_request_id = Column(Integer, ForeignKey("join_requests.id"), nullable=True)
    user_id = Column(BigInteger, nullable=False)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    decision = Column(SAEnum(DecisionEnum), nullable=False)
    score = Column(Float, default=0.0)
    matched_rules = Column(JSON, default=list)
    details = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", back_populates="logs")
    join_request = relationship("JoinRequest", back_populates="logs")


class RateLimitEntry(Base):
    __tablename__ = "rate_limit_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    action = Column(String(100), nullable=False)
    count = Column(Integer, default=1)
    window_start = Column(DateTime(timezone=True), server_default=func.now())


class BotState(Base):
    """Single-row (id=1) table tracking bot liveness for outage detection."""
    __tablename__ = "bot_state"

    id = Column(Integer, primary_key=True, default=1)
    last_heartbeat_at = Column(DateTime(timezone=True), nullable=True)
    last_started_at = Column(DateTime(timezone=True), nullable=True)
    outage_started_at = Column(DateTime(timezone=True), nullable=True)
    outage_seconds = Column(Float, nullable=True)
    recovery_completed_at = Column(DateTime(timezone=True), nullable=True)


class OutageLog(Base):
    """One row per detected outage, updated as its recovery sweep progresses."""
    __tablename__ = "outage_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    duration_seconds = Column(Float, nullable=False, default=0.0)
    groups_scanned = Column(Integer, default=0)
    requests_recovered = Column(Integer, default=0)
    sessions_extended = Column(Integer, default=0)
    members_messaged = Column(Integer, default=0)
    skipped = Column(Integer, default=0)
    details = Column(JSON, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class MemberEvent(Base):
    """Records chat_member add/remove events, mainly to catch up on ones during an outage."""
    __tablename__ = "member_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True, index=True)
    chat_id = Column(BigInteger, nullable=False)
    user_id = Column(BigInteger, nullable=False)
    old_status = Column(String(50), nullable=True)
    new_status = Column(String(50), nullable=True)
    event_at = Column(DateTime(timezone=True), nullable=False)
    during_outage = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class GroupAdmin(Base):
    """Tracks every Telegram admin who has interacted with a group's settings."""
    __tablename__ = "group_admins"
    __table_args__ = (UniqueConstraint("group_id", "admin_user_id"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    group_id = Column(Integer, ForeignKey("groups.id", ondelete="CASCADE"), nullable=False, index=True)
    admin_user_id = Column(BigInteger, nullable=False)
    admin_username = Column(String(255), nullable=True)
    admin_first_name = Column(String(255), nullable=True)
    first_seen_at = Column(DateTime(timezone=True), server_default=func.now())

    group = relationship("Group", back_populates="admins")


class UserBan(Base):
    """Manual ban issued by an admin (super-admin or group owner), independent of scoring.

    An active ban is lifted_at IS NULL. scope is one of "global", "global_remove",
    "local". group_id is the origin group for "local"/"global_remove" bans and is
    NULL for "global" bans.
    """
    __tablename__ = "user_bans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    scope = Column(String(20), nullable=False)
    group_id = Column(Integer, ForeignKey("groups.id"), nullable=True)
    reason = Column(Text, nullable=True)
    banned_by = Column(BigInteger, nullable=True)
    banned_by_name = Column(String(255), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    lifted_at = Column(DateTime(timezone=True), nullable=True)
    lifted_by = Column(BigInteger, nullable=True)
