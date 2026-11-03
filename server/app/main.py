import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app import i18n
from app.config import get_config
from app.db import get_db, utcnow
from app.models import User
from app.routes import admin
from app.security import get_csrf_token, verify_password
from app.web import (
    LANG_COOKIE,
    LoginRequired,
    current_user,
    render,
    require_user,
    safe_next,
    verify_csrf,
)

log = logging.getLogger("app")


def create_app() -> FastAPI:
    cfg = get_config()
    app = FastAPI(title=cfg.app_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        SessionMiddleware,
        secret_key=cfg.secret_key,
        session_cookie="session",
        max_age=cfg.session_max_age_seconds,
        same_site="lax",
        https_only=cfg.session_cookie_secure,
    )
    app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
    app.include_router(admin.router)

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, _exc: LoginRequired):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def home(request: Request, user: Annotated[User, Depends(require_user)]):
        return render(request, "home.html", user)

    @app.get("/login")
    def login_form(
        request: Request,
        user: Annotated[User | None, Depends(current_user)],
        next: str = "/",
    ):
        if user:
            return RedirectResponse(safe_next(next), status_code=303)
        return render(request, "login.html", None, next=safe_next(next))

    @app.post("/login", dependencies=[Depends(verify_csrf)])
    def login(
        request: Request,
        db: Annotated[Session, Depends(get_db)],
        username: Annotated[str, Form()],
        password: Annotated[str, Form()],
        next: Annotated[str, Form()] = "/",
    ):
        user = db.scalar(select(User).where(User.username == username.strip()))
        ok = verify_password(user.password_hash if user else None, password)
        if not (ok and user and user.is_active):
            log.info("failed login for %r", username)
            return render(
                request, "login.html", None, status_code=401, next=safe_next(next), error=True
            )
        user.last_login_at = utcnow()
        db.commit()
        # New session contents on login (no fixation of the pre-login session).
        request.session.clear()
        request.session["user_id"] = user.id
        get_csrf_token(request.session)
        return RedirectResponse(safe_next(next), status_code=303)

    @app.post("/logout", dependencies=[Depends(verify_csrf)])
    def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    @app.post("/lang", dependencies=[Depends(verify_csrf)])
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

    return app
