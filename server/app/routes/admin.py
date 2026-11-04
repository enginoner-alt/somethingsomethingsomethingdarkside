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
        section="users",
        users=users,
        error=error,
        min_password_length=MIN_PASSWORD_LENGTH,
    )


@router.get("/jobs")
def jobs_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
):
    return render(
        request, "admin_jobs.html", admin, section="jobs", jobs=_recent_jobs(db), queues=Queue.ALL
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
    return RedirectResponse("/admin/jobs", status_code=303)


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
        section="directory",
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
        steps = [directory.Step("directory", False, str(exc))]
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


# --- Services: WhisperX + LLM + prompts ---------------------------------------------


def _services_page(request, db, admin, verify_title=None, steps=None, saved=False, error=None):
    key = secrets.read_secret(secrets.LLM_API_KEY)
    return render(
        request,
        "admin_services.html",
        admin,
        section="services",
        s=settings_store.get_services(db),
        key_set=key is not None,
        key_fp=secrets.fingerprint(key) if key else None,
        verify_title=verify_title,
        steps=steps,
        saved=saved,
        error=error,
    )


@router.get("/services")
def services_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    saved: bool = False,
):
    return _services_page(request, db, admin, saved=saved)


@router.post("/services", dependencies=[Depends(verify_csrf)])
def services_save(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    asr_url: Annotated[str, Form()] = "",
    asr_language: Annotated[str, Form()] = "auto",
    asr_min_speakers: Annotated[int, Form()] = 0,
    asr_max_speakers: Annotated[int, Form()] = 0,
    asr_initial_prompt: Annotated[str, Form()] = "",
    asr_timeout_seconds: Annotated[int, Form()] = 10800,
    default_channel: Annotated[str, Form()] = "mix",
    llm_base_url: Annotated[str, Form()] = "",
    llm_model: Annotated[str, Form()] = "",
    llm_temperature: Annotated[float, Form()] = 0.2,
    llm_max_chars: Annotated[int, Form()] = 24000,
    llm_timeout_seconds: Annotated[int, Form()] = 300,
    auto_transcribe: Annotated[bool, Form()] = False,
    asr_diarize: Annotated[bool, Form()] = False,
):
    channel = default_channel.strip().lower() or "mix"
    urls = [u for u in (asr_url.strip(), llm_base_url.strip()) if u]
    if channel != "mix" and not channel.isdigit():
        return _services_page(request, db, admin, error="bad_channel")
    if any(not u.startswith(("http://", "https://")) for u in urls):
        return _services_page(request, db, admin, error="bad_url")
    settings_store.save_services(
        db,
        settings_store.ServicesSettings(
            asr_url=asr_url.strip().rstrip("/"),
            auto_transcribe=auto_transcribe,
            asr_language=asr_language.strip().lower() or "auto",
            asr_diarize=asr_diarize,
            asr_min_speakers=max(0, asr_min_speakers),
            asr_max_speakers=max(0, asr_max_speakers),
            asr_initial_prompt=asr_initial_prompt.strip()[:1000],
            asr_timeout_seconds=max(60, min(asr_timeout_seconds, 12 * 3600)),
            default_channel=channel,
            llm_base_url=llm_base_url.strip().rstrip("/"),
            llm_model=llm_model.strip(),
            llm_temperature=max(0.0, min(llm_temperature, 2.0)),
            llm_max_chars=max(2000, min(llm_max_chars, 2_000_000)),
            llm_timeout_seconds=max(10, min(llm_timeout_seconds, 3600)),
        ),
    )
    log.info("admin %r saved services settings", admin.username)
    return RedirectResponse("/admin/services?saved=1", status_code=303)


def _prompts_page(request, db, admin, saved=False, error=None):
    from app import insights

    return render(
        request,
        "admin_prompts.html",
        admin,
        section="prompts",
        prompts=settings_store.get_prompts(db),
        defaults=insights.DEFAULT_PROMPTS,
        kinds=insights.KINDS,
        saved=saved,
        error=error,
    )


@router.get("/prompts")
def prompts_page(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
    saved: bool = False,
):
    return _prompts_page(request, db, admin, saved=saved)


@router.post("/services/prompts", dependencies=[Depends(verify_csrf)])
async def services_prompts(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
):
    from app import insights

    form = await request.form()
    reset = form.get("reset")
    prompts = {}
    for kind in insights.KINDS:
        text = str(form.get(f"prompt_{kind}", "")).strip()
        if reset == kind or reset == "all" or not text:
            continue  # falls back to the default
        try:
            text.format(language="x", transcript="x", question="x")
        except (KeyError, IndexError, ValueError):
            return _prompts_page(request, db, admin, error="bad_prompt")
        if text != insights.DEFAULT_PROMPTS[kind]:
            prompts[kind] = text
    settings_store.save_prompts(db, prompts)
    log.info("admin %r saved prompts (%d customised)", admin.username, len(prompts))
    return RedirectResponse("/admin/prompts?saved=1", status_code=303)


@router.post("/services/verify-asr", dependencies=[Depends(verify_csrf)])
def services_verify_asr(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
):
    from app import services

    steps = services.verify_asr(settings_store.get_services(db))
    return _services_page(request, db, admin, verify_title="WhisperX", steps=steps)


@router.post("/services/verify-llm", dependencies=[Depends(verify_csrf)])
def services_verify_llm(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    admin: Annotated[User, Depends(require_admin)],
):
    from app import services

    steps = services.verify_llm(settings_store.get_services(db))
    return _services_page(request, db, admin, verify_title="LLM", steps=steps)
