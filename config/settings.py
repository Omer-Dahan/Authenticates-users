from pydantic_settings import BaseSettings
from pydantic import Field
from typing import Optional


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

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": False,
        "extra": "ignore",
    }


settings = Settings()
