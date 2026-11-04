"""Process configuration from environment variables.

Only things needed to *start* the process live here. Anything an admin should
be able to change at runtime goes in the `settings` table instead (M2+).
"""

from functools import lru_cache
from typing import Literal

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

    # Optional local test account (regular user, not admin), created on
    # startup when a password is set. Enable/disable it in the admin panel;
    # restarts never re-enable it. Changing the password here updates it.
    test_account_username: str = "test"
    test_account_password: str | None = None

    default_locale: str = "en"

    # --- Login safety -----------------------------------------------------
    # Log out after this long without any request (on top of the absolute
    # session_max_age_seconds above).
    session_idle_timeout_seconds: int = 60 * 60 * 2
    # Failed local logins allowed within the window, per (username, IP) pair
    # and per IP. Keyed by pair rather than by username alone so a stranger
    # can't lock the real admin out from somewhere else.
    login_failure_window_seconds: int = 15 * 60
    login_max_failures_per_user_ip: int = 5
    login_max_failures_per_ip: int = 20
    # "all": any local account can use a password.
    # "break_glass_only": only BOOTSTRAP_ADMIN_USERNAME can; everyone else uses SSO.
    local_login_mode: Literal["all", "break_glass_only"] = "all"

    # --- Optional OIDC SSO (Keycloak, Authentik, Entra ID, ...) -----------
    # Leave oidc_issuer empty to disable SSO entirely.
    oidc_issuer: str | None = None
    oidc_client_id: str | None = None
    oidc_client_secret: str | None = None
    oidc_scopes: str = "openid profile email"
    oidc_username_claim: str = "preferred_username"
    oidc_groups_claim: str = "groups"
    # Members of this IdP group become admins (re-evaluated at every login).
    oidc_admin_group: str | None = None
    # If set, only members of this group may log in at all.
    oidc_required_group: str | None = None
    oidc_button_label: str = "SSO"
    # Override the callback URL if the proxy setup makes auto-detection wrong,
    # e.g. https://rec.example.com/auth/oidc/callback
    oidc_redirect_uri: str | None = None

    # Extra CA certificates (internal/self-signed CAs) for outgoing HTTPS.
    # docker-compose mounts deploy/ca/ here; see app/tls.py.
    extra_ca_dir: str = "/etc/darkside/ca"

    @property
    def oidc_enabled(self) -> bool:
        return bool(self.oidc_issuer and self.oidc_client_id)

    # Worker
    worker_poll_seconds: float = 2.0
    worker_heartbeat_seconds: float = 30.0
    # A running job whose heartbeat is older than this is considered orphaned
    # (worker crashed) and is put back in the queue.
    worker_stale_after_seconds: float = 300.0


@lru_cache
def get_config() -> Config:
    return Config()
