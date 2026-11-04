"""Login, logout, language switch, and optional OIDC SSO."""

import logging
from typing import Annotated
from urllib.parse import urlencode

import httpx
from authlib.integrations.base_client.errors import OAuthError
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import directory, i18n, login_policy, oidc, throttle
from app.config import get_config
from app.db import get_db, utcnow
from app.models import User
from app.security import verify_password
from app.settings_store import get_directory
from app.web import (
    LANG_COOKIE,
    client_ip,
    current_user,
    render,
    safe_next,
    start_session,
    verify_csrf,
)

log = logging.getLogger("auth")
router = APIRouter()


def _login_page(
    request: Request,
    db: Session,
    next_: str,
    error: str | None = None,
    status_code: int = 200,
):
    return render(
        request,
        "login.html",
        None,
        status_code=status_code,
        next=safe_next(next_),
        error=error,
        policy=login_policy.current(db),
    )


@router.get("/login")
def login_form(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(current_user)],
    next: str = "/",
    error: str | None = None,
):
    if user:
        return RedirectResponse(safe_next(next), status_code=303)
    return _login_page(request, db, next, error)


@router.post("/login", dependencies=[Depends(verify_csrf)])
def login(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    """Password form: local accounts first, then Active Directory if the
    login policy allows AD passwords right now (see app/login_policy.py)."""
    cfg = get_config()
    username = username.strip()
    ip = client_ip(request)

    def fail(code: str = "credentials", status: int = 401):
        throttle.record_failure(db, username, ip, cfg)
        log.info("failed login for %r from %s (%s)", username, ip, code)
        return _login_page(request, db, next, code, status)

    if throttle.is_throttled(db, username, ip, cfg):
        log.warning("throttled login for %r from %s", username, ip)
        return _login_page(request, db, next, "throttled", 429)

    # 1. Local accounts (break-glass admin, test account, admin-created users).
    local = db.scalar(
        select(User).where(User.username == username, User.password_hash.is_not(None))
    )
    if local is not None:
        if cfg.local_login_mode == "break_glass_only" and username != cfg.bootstrap_admin_username:
            return fail()
        if not (verify_password(local.password_hash, password) and local.is_active):
            return fail()
        return _logged_in(request, db, local, "local", username, ip, next)

    # 2. Active Directory, when the policy says AD passwords count right now.
    policy = login_policy.current(db)
    if not policy.ad_passwords_allowed:
        if policy.ldap_configured and policy.oidc_configured and policy.oidc_ok:
            # AD is set up, but SSO works and is preferred: send them there.
            return _login_page(request, db, next, "use_sso", 401)
        return fail()

    settings = get_directory(db)
    try:
        duser = directory.Directory(settings).authenticate(username, password)
    except directory.DirectoryUnavailable as exc:
        log.warning("AD unavailable during login for %r: %s", username, exc)
        login_policy.clear_health_cache()
        return _login_page(request, db, next, "directory_unavailable", 503)
    if duser is None:
        return fail()
    user, error = login_policy.upsert_ldap_user(db, duser, settings)
    if user is None:
        return fail(error or "credentials", 403)
    return _logged_in(request, db, user, "ldap", username, ip, next)


def _logged_in(request: Request, db: Session, user: User, via: str, username, ip, next_):
    throttle.clear_failures(db, username, ip)
    user.last_login_at = utcnow()
    db.commit()
    start_session(request, user, via)
    return RedirectResponse(safe_next(next_), status_code=303)


@router.post("/logout", dependencies=[Depends(verify_csrf)])
async def logout(request: Request):
    via = request.session.get("via")
    request.session.clear()
    cfg = get_config()
    if via == "oidc" and cfg.oidc_enabled:
        # End the IdP session too, otherwise "Sign in with SSO" logs straight back in.
        try:
            metadata = await oidc.get_oauth().idp.load_server_metadata()
            end_session = metadata.get("end_session_endpoint")
        except Exception:
            log.exception("could not load OIDC metadata for logout")
            end_session = None
        if end_session:
            params = urlencode(
                {
                    "client_id": cfg.oidc_client_id,
                    "post_logout_redirect_uri": str(request.url_for("login_form")),
                }
            )
            return RedirectResponse(f"{end_session}?{params}", status_code=303)
    return RedirectResponse("/login", status_code=303)


@router.post("/lang", dependencies=[Depends(verify_csrf)])
def set_language(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    user: Annotated[User | None, Depends(current_user)],
    locale: Annotated[str, Form()],
    next: Annotated[str, Form()] = "/",
):
    response = RedirectResponse(safe_next(next), status_code=303)
    if locale in i18n.load_locales():
        if user:
            user.locale = locale
            db.commit()
        response.set_cookie(
            LANG_COOKIE,
            locale,
            max_age=60 * 60 * 24 * 365,
            samesite="lax",
            secure=get_config().session_cookie_secure,
        )
    return response


# --- OIDC ---------------------------------------------------------------------


def _require_oidc() -> None:
    if not get_config().oidc_enabled:
        raise HTTPException(status_code=404)


@router.get("/auth/oidc/login", dependencies=[Depends(_require_oidc)])
async def oidc_login(request: Request, db: Annotated[Session, Depends(get_db)], next: str = "/"):
    cfg = get_config()
    request.session["oidc_next"] = safe_next(next)
    redirect_uri = cfg.oidc_redirect_uri or str(request.url_for("oidc_callback"))
    try:
        client = await oidc.prepare_client()
        return await client.authorize_redirect(request, redirect_uri)
    except Exception:
        # IdP unreachable or misconfigured: say so, and leave the local
        # (break-glass) login usable on the same page.
        log.exception("could not start OIDC login")
        return _login_page(request, db, next, "sso_unavailable", 503)


@router.get("/auth/oidc/callback", dependencies=[Depends(_require_oidc)])
async def oidc_callback(request: Request, db: Annotated[Session, Depends(get_db)]):
    cfg = get_config()
    next_ = request.session.get("oidc_next", "/")
    try:
        client = await oidc.prepare_client()
        token = await client.authorize_access_token(request)
        claims = dict(token.get("userinfo") or {})
        if cfg.oidc_groups_claim not in claims:
            # Some IdPs only put groups in the userinfo endpoint, not the ID token.
            claims.update(await client.userinfo(token=token))
    except OAuthError as exc:
        log.warning("OIDC callback failed: %s", exc)
        return _login_page(request, db, next_, "sso_failed", 401)
    except httpx.HTTPError as exc:
        # IdP slow or unreachable while exchanging the code: a clear message,
        # not a 500. The user can retry or use the break-glass login.
        log.warning("OIDC callback could not reach the IdP: %r", exc)
        return _login_page(request, db, next_, "sso_unavailable", 503)

    result = oidc.upsert_user(db, claims, cfg)
    if result.user is None:
        log.info("OIDC login refused (%s) for sub=%s", result.error, claims.get("sub"))
        return _login_page(request, db, next_, result.error, 403)

    start_session(request, result.user, "oidc")
    return RedirectResponse(safe_next(next_), status_code=303)
