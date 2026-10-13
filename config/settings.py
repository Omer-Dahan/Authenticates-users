from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # Telegram
    bot_token: str = Field(..., env="BOT_TOKEN")
    super_admin_id: int = Field(..., env="SUPER_ADMIN_ID")
    tracking_channel_id: Optional[int] = Field(default=None, env="TRACKING_CHANNEL_ID")

    # Database
    database_url: str = Field(
        default="sqlite+aiosqlite:///./data/moderation.db",
        env="DATABASE_URL",
    )

    # Logging
    log_level: str = Field(default="INFO", env="LOG_LEVEL")
    log_file: str = Field(default="logs/moderation.log", env="LOG_FILE")

    # Rate Limiting
    rate_limit_requests: int = Field(default=10, env="RATE_LIMIT_REQUESTS")
    rate_limit_window: int = Field(default=60, env="RATE_LIMIT_WINDOW")

    # Raid Protection
    raid_threshold: int = Field(default=10, env="RAID_THRESHOLD")
    raid_window: int = Field(default=30, env="RAID_WINDOW")

    # Outage / downtime recovery
    outage_threshold_seconds: int = Field(default=180, env="OUTAGE_THRESHOLD_SECONDS")
    heartbeat_interval_seconds: int = Field(default=60, env="HEARTBEAT_INTERVAL_SECONDS")
    recovery_grace_seconds: int = Field(default=30, env="RECOVERY_GRACE_SECONDS")

    # Join-request reconciliation sweep (bot's join_requests rows vs what
    # Telegram can actually confirm - see moderation/actions.py). Default is
    # 0 (disabled): a row the bot cannot prove is resolved (an "undetermined"
    # outcome) might still be a genuine pending request the app still shows,
    # and auto-closing it by age alone is exactly the silent-loss failure the
    # owner reported. An admin who explicitly wants age-based auto-close for
    # undetermined rows can still opt in by setting this above 0.
    reconciliation_stale_days: int = Field(default=0, env="RECONCILIATION_STALE_DAYS")
    reconciliation_daily_sweep_enabled: bool = Field(default=True, env="RECONCILIATION_DAILY_SWEEP_ENABLED")
    reconciliation_daily_interval_seconds: int = Field(default=86400, env="RECONCILIATION_DAILY_INTERVAL_SECONDS")

    # Real pending-join-request count, read directly from Telegram over
    # MTProto (see telegram_mtproto/). Optional: without API_ID/API_HASH or
    # the telethon package installed, this feature is simply off - no crash,
    # just one log line at startup (see telegram_mtproto/pending_counts.py).
    telegram_api_id: Optional[int] = Field(default=None, env="TELEGRAM_API_ID")
    telegram_api_hash: Optional[str] = Field(default=None, env="TELEGRAM_API_HASH")
    telegram_pending_enabled: bool = Field(default=True, env="TELEGRAM_PENDING_ENABLED")
    telegram_pending_refresh_seconds: int = Field(default=600, env="TELEGRAM_PENDING_REFRESH_SECONDS")

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


settings = Settings()
