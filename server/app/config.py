"""Process configuration from environment variables.

Only things needed to *start* the process live here. Anything an admin should
be able to change at runtime goes in the `settings` table instead (M2+).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Config(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Darkside"
    database_url: str = "postgresql+psycopg://darkside:darkside@postgres:5432/darkside"

    # Signs the session cookie. Must be long and random in production.
    secret_key: str = Field(min_length=32)
    # Cookies only over HTTPS. Turn off only for tests / plain-HTTP debugging.
    session_cookie_secure: bool = True
    session_max_age_seconds: int = 60 * 60 * 12

    # Break-glass local admin, created on startup if missing (never overwritten
    # unless bootstrap_admin_reset is true).
    bootstrap_admin_username: str = "admin"
    bootstrap_admin_password: str | None = None
    bootstrap_admin_reset: bool = False

    default_locale: str = "en"

    # Worker
    worker_poll_seconds: float = 2.0
    worker_heartbeat_seconds: float = 30.0
    # A running job whose heartbeat is older than this is considered orphaned
    # (worker crashed) and is put back in the queue.
    worker_stale_after_seconds: float = 300.0


@lru_cache
def get_config() -> Config:
    return Config()
