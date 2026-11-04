import logging
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app import branding, i18n
from app.config import get_config
from app.models import User
from app.routes import admin, auth, recordings, transcripts
from app.tls import ssl_context
from app.web import LoginRequired, render, require_user, resolve_locale


def create_app() -> FastAPI:
    cfg = get_config()
    # uvicorn only configures its own loggers; show the app's INFO lines too
    # (failed logins, "trusting extra CA certificate ...", etc.).
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s: %(message)s")
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
    app.include_router(auth.router)
    app.include_router(admin.router)
    app.include_router(recordings.router)
    app.include_router(transcripts.router)

    if cfg.oidc_enabled:
        ssl_context()  # load trusted CAs now, so the startup log lists them

    @app.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, _exc: LoginRequired):
        if request.url.path.startswith("/api/"):
            # The recorder needs to know the session ended, not get a login page.
            return JSONResponse({"error": "login_required"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.get("/branding/logo")
    def logo():
        path = branding.logo_path()
        if path is None:
            return Response(status_code=404)
        return FileResponse(path, headers={"Cache-Control": "public, max-age=300"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/")
    def home(request: Request, user: Annotated[User, Depends(require_user)]):
        js = i18n.strings(resolve_locale(request, user), "rec.", "err.")
        return render(request, "home.html", user, js_strings=js)

    @app.get("/record")
    def record(request: Request, user: Annotated[User, Depends(require_user)]):
        js = i18n.strings(resolve_locale(request, user), "rec.", "err.")
        return render(request, "record.html", user, js_strings=js)

    return app
