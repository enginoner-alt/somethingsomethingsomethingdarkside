"""Optional OIDC single sign-on.

The app only speaks OIDC. LDAP / Active Directory, MFA, passkeys and lockout
policies all live in the identity provider (Keycloak, Authentik, Entra ID, ...).
"""

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from authlib.integrations.starlette_client import OAuth
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config, get_config
from app.db import utcnow
from app.models import AuthSource, User
from app.tls import ssl_context


@lru_cache
def get_oauth() -> OAuth:
    cfg = get_config()
    oauth = OAuth()
    oauth.register(
        "idp",
        server_metadata_url=cfg.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration",
        client_id=cfg.oidc_client_id,
        client_secret=cfg.oidc_client_secret,
        client_kwargs={
            "scope": cfg.oidc_scopes,
            "code_challenge_method": "S256",
            # Public CAs + anything in deploy/ca/ (internal CA for the IdP).
            "verify": ssl_context(),
            # httpx's default is 5 s; a busy or cold IdP can take longer.
            "timeout": 15.0,
        },
    )
    return oauth


def normalize_groups(raw: Any) -> set[str]:
    """Accept a list or a single string; strip Keycloak's leading '/' (full path)."""
    if raw is None:
        return set()
    items = [raw] if isinstance(raw, str) else list(raw)
    return {str(g).strip().lstrip("/") for g in items if str(g).strip()}


@dataclass
class OidcResult:
    user: User | None = None
    error: str | None = None  # login.error.<code>


def upsert_user(db: Session, claims: dict[str, Any], cfg: Config) -> OidcResult:
    """Find or create the local user for a verified set of OIDC claims.

    Users are keyed by (issuer, subject). A username that already belongs to
    another account is never taken over, so an IdP user called "admin" can't
    become the local break-glass admin.
    """
    issuer, subject = claims.get("iss"), claims.get("sub")
    if not issuer or not subject:
        return OidcResult(error="sso_failed")

    groups = normalize_groups(claims.get(cfg.oidc_groups_claim))
    if cfg.oidc_required_group and cfg.oidc_required_group.lstrip("/") not in groups:
        return OidcResult(error="not_allowed")

    username = str(claims.get(cfg.oidc_username_claim) or subject).strip()[:150]
    display_name = claims.get("name") or None

    user = db.scalar(select(User).where(User.oidc_issuer == issuer, User.oidc_subject == subject))
    if user is None:
        if db.scalar(select(User).where(User.username == username)):
            return OidcResult(error="conflict")
        user = User(
            username=username,
            auth_source=AuthSource.OIDC,
            oidc_issuer=issuer,
            oidc_subject=subject,
            is_admin=False,
        )
        db.add(user)
    elif not user.is_active:
        return OidcResult(error="disabled")
    elif user.username != username and not db.scalar(select(User).where(User.username == username)):
        user.username = username  # renamed in the IdP

    user.display_name = display_name
    if cfg.oidc_admin_group:
        user.is_admin = cfg.oidc_admin_group.lstrip("/") in groups
    user.last_login_at = utcnow()
    db.commit()
    return OidcResult(user=user)
