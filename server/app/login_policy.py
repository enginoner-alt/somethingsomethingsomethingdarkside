"""Which login method applies right now, and AD account handling.

Owner's rule:
  - SSO and AD both configured and working -> the preferred one (default SSO)
  - only one of them working                -> that one
  - neither                                 -> only local accounts (break-glass)

"Working" is checked live and cached for HEALTH_TTL seconds, so a login
doesn't wait on a slow IdP every time.
"""

import logging
import threading
import time
from dataclasses import dataclass

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import directory
from app.config import get_config
from app.db import utcnow
from app.models import AuthSource, User
from app.oidc import group_matches
from app.settings_store import DirectorySettings, get_directory, get_login
from app.tls import ssl_context

log = logging.getLogger("login_policy")

HEALTH_TTL = 30.0
_cache: dict[str, tuple[float, bool, str]] = {}
_lock = threading.Lock()


def _cached(key: str, check) -> tuple[bool, str]:
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < HEALTH_TTL:
            return hit[1], hit[2]
    ok, detail = check()
    with _lock:
        _cache[key] = (time.monotonic(), ok, detail)
    return ok, detail


def clear_health_cache() -> None:
    with _lock:
        _cache.clear()


def check_oidc() -> tuple[bool, str]:
    """Fresh check: can we fetch the IdP's discovery document?"""
    cfg = get_config()
    if not cfg.oidc_enabled:
        return False, "not configured"
    url = cfg.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
    try:
        r = httpx.get(url, verify=ssl_context(), timeout=3.0)
        r.raise_for_status()
        meta = r.json()
        if not meta.get("authorization_endpoint") or not meta.get("token_endpoint"):
            return False, "discovery document is incomplete"
        return True, f"reachable (issuer {meta.get('issuer')})"
    except (httpx.HTTPError, ValueError) as exc:
        return False, f"{exc.__class__.__name__}: {exc}"


def check_ldap(s: DirectorySettings) -> tuple[bool, str]:
    """Fresh check: does the service account bind?"""
    if not s.enabled:
        return False, "disabled"
    problems = directory.config_problems(s)
    if problems:
        return False, "; ".join(problems)
    try:
        conn = directory.Directory(s).service_connection()
        server = conn.server.host
        conn.unbind()
        return True, f"service account bound on {server}"
    except directory.DirectoryUnavailable as exc:
        return False, str(exc)


def oidc_working() -> bool:
    return _cached("oidc", check_oidc)[0]


def ldap_working(s: DirectorySettings) -> bool:
    key = f"ldap:{tuple(s.servers)}:{s.bind_dn}:{s.enabled}"
    return _cached(key, lambda: check_ldap(s))[0]


@dataclass
class Policy:
    oidc_configured: bool
    oidc_ok: bool
    ldap_configured: bool
    ldap_ok: bool
    preferred: str

    @property
    def ad_passwords_allowed(self) -> bool:
        """May the password form be used with an AD account right now?"""
        if not (self.ldap_configured and self.ldap_ok):
            return False
        if self.preferred == "ldap":
            return True
        return not (self.oidc_configured and self.oidc_ok)

    @property
    def sso_down_ad_up(self) -> bool:
        return self.oidc_configured and not self.oidc_ok and self.ad_passwords_allowed


def current(db: Session) -> Policy:
    s = get_directory(db)
    ldap_configured = directory.is_configured(s)
    oidc_configured = get_config().oidc_enabled
    return Policy(
        oidc_configured=oidc_configured,
        oidc_ok=oidc_working() if oidc_configured else False,
        ldap_configured=ldap_configured,
        ldap_ok=ldap_working(s) if ldap_configured else False,
        preferred=get_login(db).preferred_method,
    )


def upsert_ldap_user(
    db: Session, duser: directory.DirectoryUser, s: DirectorySettings
) -> tuple[User | None, str | None]:
    """Find or create the app user for an authenticated AD user.

    Keyed by objectGUID, so the same person signing in via SSO (with the
    GUID claim configured) and via AD is one account. A username already used
    by a different account is never taken over."""
    if s.required_group and not group_matches(s.required_group, duser.groups):
        log.info("AD login %r refused: not in %r", duser.username, s.required_group)
        return None, "not_allowed"

    user = db.scalar(select(User).where(User.ad_guid == duser.guid))
    if user is None:
        if db.scalar(select(User).where(User.username == duser.username)):
            log.warning("AD login %r refused: username belongs to another account", duser.username)
            return None, "conflict"
        user = User(username=duser.username, auth_source=AuthSource.LDAP, ad_guid=duser.guid)
        db.add(user)
    elif not user.is_active:
        return None, "disabled"

    user.display_name = duser.display_name or user.display_name
    if s.admin_group:
        user.is_admin = group_matches(s.admin_group, duser.groups)
    user.last_login_at = utcnow()
    db.commit()
    log.info("AD login %r: groups=%s admin=%s", duser.username, sorted(duser.groups), user.is_admin)
    return user, None


def verify_oidc() -> list[directory.Step]:
    """Admin "Verify SSO" button."""
    cfg = get_config()
    steps = [
        directory.Step(
            "Settings",
            cfg.oidc_enabled,
            "OIDC_ISSUER and OIDC_CLIENT_ID set"
            if cfg.oidc_enabled
            else "OIDC_ISSUER / OIDC_CLIENT_ID not set in .env",
        )
    ]
    if not cfg.oidc_enabled:
        return steps
    ok, detail = check_oidc()
    steps.append(directory.Step("Discovery document", ok, detail))
    if ok:
        url = cfg.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration"
        meta = httpx.get(url, verify=ssl_context(), timeout=3.0).json()
        methods = meta.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        steps.append(directory.Step("Client secret methods offered", True, ", ".join(methods)))
        steps.append(
            directory.Step(
                "Logout endpoint",
                bool(meta.get("end_session_endpoint")),
                meta.get("end_session_endpoint") or "none: logout is app-only",
            )
        )
    return steps
