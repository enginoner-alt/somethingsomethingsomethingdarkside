"""Optional OIDC single sign-on.

The app only speaks OIDC. LDAP / Active Directory, MFA, passkeys and lockout
policies all live in the identity provider (Keycloak, Authentik, Entra ID, ...).
"""

import logging
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

log = logging.getLogger("oidc")


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
    if cfg.oidc_token_auth_method != "auto":
        oauth.idp.client_kwargs["token_endpoint_auth_method"] = cfg.oidc_token_auth_method
    return oauth


def pick_token_auth_method(metadata: dict[str, Any]) -> str:
    """How to send the client secret, from the IdP's discovery document.

    Prefer HTTP Basic (the OIDC default); use the form body only when the IdP
    says it doesn't accept Basic. No list advertised means Basic per the spec.
    """
    supported = metadata.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
    if "client_secret_basic" in supported:
        return "client_secret_basic"
    if "client_secret_post" in supported:
        return "client_secret_post"
    return "client_secret_basic"


async def prepare_client():
    """The registered client, with the token auth method resolved for "auto"."""
    client = get_oauth().idp
    if "token_endpoint_auth_method" not in client.client_kwargs:
        method = pick_token_auth_method(await client.load_server_metadata())
        client.client_kwargs["token_endpoint_auth_method"] = method
        log.info("OIDC token endpoint auth method: %s (auto)", method)
    return client


def normalize_groups(raw: Any) -> set[str]:
    """Accept a list or a single string; strip Keycloak's leading '/' (full path)."""
    if raw is None:
        return set()
    items = [raw] if isinstance(raw, str) else list(raw)
    return {str(g).strip().lstrip("/") for g in items if str(g).strip()}


def _split_dn(dn: str) -> list[tuple[str, str]] | None:
    """Split an LDAP DN into (attribute, value) pairs, honouring backslash
    escapes ("CN=Smith\\, John,OU=..."). None if it isn't DN-shaped."""
    rdns, buf, escaped = [], "", False
    for ch in dn:
        if escaped:
            buf += ch
            escaped = False
        elif ch == "\\":
            buf += ch
            escaped = True
        elif ch == ",":
            rdns.append(buf)
            buf = ""
        else:
            buf += ch
    rdns.append(buf)
    pairs = []
    for rdn in rdns:
        attr, sep, value = rdn.partition("=")
        if not sep or not attr.strip() or not attr.strip().replace("-", "").isalnum():
            return None
        pairs.append((attr.strip().lower(), value.strip().replace("\\", "")))
    return pairs


def _canonical(name: str) -> str:
    pairs = _split_dn(name)
    if pairs and len(pairs) > 1:
        return ",".join(f"{a}={v.casefold()}" for a, v in pairs)
    return name.casefold()


def group_matches(configured: str, groups: set[str]) -> bool:
    """Is the user in `configured`?

    - A plain name ("darkside-admins") matches that exact group name, or an
      LDAP DN whose first part is CN=darkside-admins, e.g.
      "CN=darkside-admins,OU=Groups,DC=corp,DC=local" (what AD-backed IdPs
      like PingFederate or NetIQ often send).
    - A full DN matches only that exact DN (use this to pin one specific OU).
    - IDs such as Entra group GUIDs match exactly.
    Comparison ignores case, like AD does.
    """
    want = configured.strip().lstrip("/")
    if not want:
        return False
    want_canon = _canonical(want)
    plain = "=" not in want
    for group in groups:
        if _canonical(group) == want_canon:
            return True
        if plain:
            pairs = _split_dn(group)
            if pairs and pairs[0][0] == "cn" and pairs[0][1].casefold() == want.casefold():
                return True
    return False


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
    # Logged so "why can't X log in / isn't admin?" can be answered from the
    # log: it shows exactly what the IdP sent in the configured claim.
    log.info(
        "OIDC login %r: %s=%s",
        claims.get(cfg.oidc_username_claim) or subject,
        cfg.oidc_groups_claim,
        sorted(groups),
    )
    if cfg.oidc_required_group and not group_matches(cfg.oidc_required_group, groups):
        log.info("refused: not in required group %r", cfg.oidc_required_group)
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
        user.is_admin = group_matches(cfg.oidc_admin_group, groups)
    user.last_login_at = utcnow()
    db.commit()
    return OidcResult(user=user)
