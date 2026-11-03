"""Admin panel. Manages users, settings and jobs — never other users' content (D12)."""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import jobs
from app.db import get_db
from app.models import Job, Queue, User
from app.security import hash_password
from app.web import render, require_admin, verify_csrf

router = APIRouter(prefix="/admin")

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
