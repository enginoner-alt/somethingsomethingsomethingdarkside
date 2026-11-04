"""Shared helpers for HTML routes: current user, locale, CSRF, rendering."""

import time
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, Form, HTTPException, Request
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import i18n
from app.config import get_config
from app.db import get_db
from app.models import User
from app.security import check_csrf, get_csrf_token

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

LANG_COOKIE = "lang"


class LoginRequired(Exception):
    """Raised by `require_user`; turned into a redirect to /login."""


def start_session(request: Request, user: User, method: str, **extra: object) -> None:
    """Fresh session contents on login (no fixation of the pre-login session)."""
    request.session.clear()
    request.session.update(
        {"user_id": user.id, "v": user.auth_version, "seen": int(time.time()), "via": method}
    )
    request.session.update(extra)
    get_csrf_token(request.session)


def current_user(request: Request, db: Annotated[Session, Depends(get_db)]) -> User | None:
    session = request.session
    user_id = session.get("user_id")
    if user_id is None:
        return None
    now = int(time.time())
    idle = now - int(session.get("seen", 0))
    user = db.get(User, user_id)
    if (
        user is None
        or not user.is_active
        # Revoked: user disabled/re-enabled, password reset, etc.
        or session.get("v") != user.auth_version
        or idle > get_config().session_idle_timeout_seconds
    ):
        session.clear()
        return None
    session["seen"] = now
    return user


def require_user(user: Annotated[User | None, Depends(current_user)]) -> User:
    if user is None:
        raise LoginRequired
    return user


def require_admin(user: Annotated[User, Depends(require_user)]) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=403)
    return user


def verify_csrf(request: Request, csrf: Annotated[str | None, Form()] = None) -> None:
    if not check_csrf(request.session, csrf):
        raise HTTPException(status_code=400, detail="Invalid or missing CSRF token")


def verify_csrf_header(request: Request) -> None:
    """For fetch() calls from our own pages: the CSRF token travels in the
    X-CSRF-Token header (forms use the hidden field instead)."""
    if not check_csrf(request.session, request.headers.get("x-csrf-token")):
        raise HTTPException(status_code=403, detail="Invalid or missing CSRF token")


def resolve_locale(request: Request, user: User | None) -> str:
    available = i18n.load_locales()
    for candidate in (user.locale if user else None, request.cookies.get(LANG_COOKIE)):
        if candidate in available:
            return candidate
    return i18n.negotiate(request.headers.get("accept-language"), get_config().default_locale)


def client_ip(request: Request) -> str:
    # uvicorn runs with --proxy-headers and the api port is only reachable
    # through Caddy, so this is the real client address.
    return (request.client.host if request.client else None) or "unknown"


def safe_next(target: str | None) -> str:
    """Only allow same-site relative redirects."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return "/"


def render(
    request: Request,
    name: str,
    user: User | None,
    status_code: int = 200,
    **context: Any,
):
    locale = resolve_locale(request, user)
    return templates.TemplateResponse(
        request,
        name,
        {
            "app_name": get_config().app_name,
            "cfg": get_config(),
            "user": user,
            "locale": locale,
            "locales": i18n.available_locales(),
            "csrf_token": get_csrf_token(request.session),
            "t": lambda key, **params: i18n.translate(locale, key, **params),
            **context,
        },
        status_code=status_code,
    )
