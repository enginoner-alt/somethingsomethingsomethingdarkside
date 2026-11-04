"""Admin panel. Manages users, settings and jobs — never other users' content (D12)."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import directory, jobs, login_policy, secrets, settings_store
from app.db import get_db
from app.models import Job, Queue, User
from app.security import hash_password
from app.web import render, require_admin, verify_csrf

router = APIRouter(prefix="/admin")
log = logging.getLogger("admin")

MIN_PASSWORD_LENGTH = 10


def _job_dict(job: Job) -> dict:
    return {
        "id": job.id,
        "queue": job.queue,
        "type": job.type,
        "status": job.status,
        "attempts": job.attempts,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "result": job.result,
        "error": job.error,
    }


def _recent_jobs(db: Session) -> list[Job]:
    return list(db.scalars(select(Job).order_by(Job.id.desc()).limit(20)))


@router.get("")
def admin_index(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    error: str | None = None,
):
    users = list(db.scalars(select(User).order_by(User.username)))
    return render(
        request,
        "admin.html",
        admin,
        users=users,
        jobs=_recent_jobs(db),
        queues=Queue.ALL,
        error=error,
        min_password_length=MIN_PASSWORD_LENGTH,
    )


@router.post("/users", dependencies=[Depends(verify_csrf)])
def create_user(
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    is_admin: Annotated[bool, Form()] = False,
):
    username = username.strip()
    if not username or len(password) < MIN_PASSWORD_LENGTH:
        return RedirectResponse("/admin?error=invalid_user", status_code=303)
    if db.scalar(select(User).where(User.username == username)):
        return RedirectResponse("/admin?error=user_exists", status_code=303)
    db.add(User(username=username, password_hash=hash_password(password), is_admin=is_admin))
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/users/{user_id}/toggle-active", dependencies=[Depends(verify_csrf)])
def toggle_active(
    user_id: int,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
):
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404)
    if user.id == admin.id:
        return RedirectResponse("/admin?error=self_disable", status_code=303)
    user.is_active = not user.is_active
    user.auth_version += 1  # end their sessions now, not at idle timeout
    db.commit()
    return RedirectResponse("/admin", status_code=303)


@router.post("/jobs/test", dependencies=[Depends(verify_csrf)])
def test_job(
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    queue: Annotated[str, Form()],
):
    if queue not in Queue.ALL:
        raise HTTPException(status_code=400)
    jobs.enqueue(db, queue, "noop", {"seconds": 2}, user_id=admin.id, max_attempts=1)
    return RedirectResponse("/admin#jobs", status_code=303)


@router.get("/jobs.json")
def jobs_json(
    db: Annotated[Session, Depends(get_db)],
    _admin: Annotated[User, Depends(require_admin)],
):
    return [_job_dict(j) for j in _recent_jobs(db)]


# --- Directory (AD) and login methods ------------------------------------------


def _directory_page(request, db, admin, verify_title=None, steps=None, saved=False, error=None):
    s = settings_store.get_directory(db)
    secret = secrets.read_secret(secrets.LDAP_BIND_PASSWORD)
    return render(
        request,
        "admin_directory.html",
        admin,
        d=s,
        login=settings_store.get_login(db),
        policy=login_policy.current(db),
        problems=directory.config_problems(s) if s.enabled else [],
        secret_set=secret is not None,
        secret_fp=secrets.fingerprint(secret) if secret else None,
        verify_title=verify_title,
        steps=steps,
        saved=saved,
        error=error,
    )


@router.get("/directory")
def directory_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    saved: bool = False,
):
    return _directory_page(request, db, admin, saved=saved)


@router.post("/directory", dependencies=[Depends(verify_csrf)])
def directory_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    servers: Annotated[str, Form()] = "",
    bind_dn: Annotated[str, Form()] = "",
    search_base: Annotated[str, Form()] = "",
    username_attribute: Annotated[str, Form()] = "sAMAccountName",
    user_filter: Annotated[str, Form()] = "",
    group_filter: Annotated[str, Form()] = "",
    group_name_attribute: Annotated[str, Form()] = "sAMAccountName",
    required_group: Annotated[str, Form()] = "",
    admin_group: Annotated[str, Form()] = "",
    timeout_seconds: Annotated[int, Form()] = 5,
    preferred_method: Annotated[str, Form()] = "oidc",
    enabled: Annotated[bool, Form()] = False,
    start_tls: Annotated[bool, Form()] = False,
    nested_groups: Annotated[bool, Form()] = False,
):
    defaults = settings_store.DirectorySettings()
    s = settings_store.DirectorySettings(
        enabled=enabled,
        servers=[line.strip() for line in servers.splitlines() if line.strip()],
        start_tls=start_tls,
        bind_dn=bind_dn.strip(),
        search_base=search_base.strip(),
        username_attribute=username_attribute.strip() or defaults.username_attribute,
        user_filter=user_filter.strip() or defaults.user_filter,
        group_filter=group_filter.strip() or defaults.group_filter,
        group_name_attribute=group_name_attribute.strip() or defaults.group_name_attribute,
        nested_groups=nested_groups,
        required_group=required_group.strip(),
        admin_group=admin_group.strip(),
        timeout_seconds=max(1, min(timeout_seconds, 30)),
    )
    if preferred_method not in ("oidc", "ldap"):
        raise HTTPException(status_code=400)
    bad = [u for u in s.servers if not (u.startswith("ldaps://") or u.startswith("ldap://"))]
    if bad:
        return _directory_page(request, db, admin, error="bad_url")
    settings_store.save_directory(db, s)
    settings_store.save_login(db, settings_store.LoginSettings(preferred_method=preferred_method))
    login_policy.clear_health_cache()
    log.info("admin %r saved directory settings (enabled=%s)", admin.username, s.enabled)
    return RedirectResponse("/admin/directory?saved=1", status_code=303)


@router.post("/directory/verify-ad", dependencies=[Depends(verify_csrf)])
def directory_verify_ad(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    test_username: Annotated[str, Form()] = "",
    test_password: Annotated[str, Form()] = "",
):
    s = settings_store.get_directory(db)
    try:
        steps = directory.verify(s, test_username, test_password)
    except directory.DirectoryUnavailable as exc:
        steps = [directory.Step("Directory", False, str(exc))]
    login_policy.clear_health_cache()
    return _directory_page(request, db, admin, verify_title="AD", steps=steps)


@router.post("/directory/verify-sso", dependencies=[Depends(verify_csrf)])
def directory_verify_sso(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
):
    steps = login_policy.verify_oidc()
    login_policy.clear_health_cache()
    return _directory_page(request, db, admin, verify_title="SSO", steps=steps)
