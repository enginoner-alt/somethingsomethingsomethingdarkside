"""Postgres-backed job queue.

Workers claim jobs with `SELECT ... FOR UPDATE SKIP LOCKED`, so several
workers can share a queue safely. No Redis, no broker.
"""

from datetime import timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Job, JobStatus


def enqueue(
    db: Session,
    queue: str,
    type_: str,
    payload: dict[str, Any] | None = None,
    user_id: int | None = None,
    max_attempts: int = 3,
) -> Job:
    job = Job(
        queue=queue,
        type=type_,
        payload=payload or {},
        user_id=user_id,
        max_attempts=max_attempts,
        status=JobStatus.QUEUED,
    )
    db.add(job)
    db.commit()
    return job


def requeue_stale(db: Session, queue: str, stale_after_seconds: float) -> int:
    """Put orphaned running jobs (worker died) back in the queue, or fail them
    if they're out of attempts. Returns how many jobs were touched."""
    cutoff = utcnow() - timedelta(seconds=stale_after_seconds)
    stale = or_(Job.heartbeat_at.is_(None), Job.heartbeat_at < cutoff)
    base = select(Job).where(Job.queue == queue, Job.status == JobStatus.RUNNING, stale)
    jobs = db.scalars(base.with_for_update(skip_locked=True)).all()
    for job in jobs:
        job.locked_by = None
        if job.attempts >= job.max_attempts:
            job.status = JobStatus.FAILED
            job.error = (job.error or "") + "\nworker lost; out of attempts"
            job.finished_at = utcnow()
        else:
            job.status = JobStatus.QUEUED
    db.commit()
    return len(jobs)


def claim(db: Session, queue: str, worker_id: str) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.queue == queue, Job.status == JobStatus.QUEUED)
        .order_by(Job.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = db.scalars(stmt).first()
    if job is None:
        db.rollback()
        return None
    now = utcnow()
    job.status = JobStatus.RUNNING
    job.locked_by = worker_id
    job.attempts += 1
    job.started_at = now
    job.heartbeat_at = now
    job.error = None
    db.commit()
    return job


def heartbeat(db: Session, job_id: int, worker_id: str) -> None:
    db.execute(
        update(Job)
        .where(Job.id == job_id, Job.locked_by == worker_id, Job.status == JobStatus.RUNNING)
        .values(heartbeat_at=utcnow())
    )
    db.commit()


def complete(db: Session, job: Job, result: dict[str, Any] | None) -> None:
    job.status = JobStatus.DONE
    job.result = result
    job.locked_by = None
    job.finished_at = utcnow()
    db.commit()


def fail(db: Session, job: Job, error: str) -> None:
    """Retry if attempts remain, otherwise mark failed."""
    job.error = error
    job.locked_by = None
    if job.attempts >= job.max_attempts:
        job.status = JobStatus.FAILED
        job.finished_at = utcnow()
    else:
        job.status = JobStatus.QUEUED
    db.commit()
