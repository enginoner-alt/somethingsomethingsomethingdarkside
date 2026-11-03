from datetime import timedelta

from app import jobs
from app.db import get_sessionmaker, utcnow
from app.models import Job, JobStatus
from app.worker import run_once


def test_claim_respects_queue_and_order(db):
    a = jobs.enqueue(db, "gpu", "noop")
    jobs.enqueue(db, "llm", "noop")
    b = jobs.enqueue(db, "gpu", "noop")

    first = jobs.claim(db, "gpu", "w1")
    second = jobs.claim(db, "gpu", "w1")
    assert (first.id, second.id) == (a.id, b.id)
    assert jobs.claim(db, "gpu", "w1") is None
    assert first.status == JobStatus.RUNNING and first.attempts == 1


def test_concurrent_claims_skip_locked(db):
    """Two workers claiming at the same moment never get the same job."""
    jobs.enqueue(db, "gpu", "noop")
    jobs.enqueue(db, "gpu", "noop")
    make = get_sessionmaker()
    with make() as s1, make() as s2:
        # s1 holds its row lock (no commit yet) while s2 claims.
        from sqlalchemy import select

        locked = s1.scalars(
            select(Job).where(Job.status == "queued").order_by(Job.id).limit(1).with_for_update()
        ).first()
        other = jobs.claim(s2, "gpu", "w2")
        assert other is not None and other.id != locked.id
        s1.rollback()


def test_failure_retries_then_fails(db):
    job = jobs.enqueue(db, "gpu", "noop", {"seconds": 0, "fail": True}, max_attempts=2)
    assert run_once(db, "gpu", "w")
    db.refresh(job)
    assert job.status == JobStatus.QUEUED and "asked to fail" in job.error
    assert run_once(db, "gpu", "w")
    db.refresh(job)
    assert job.status == JobStatus.FAILED and job.attempts == 2


def test_unknown_job_type_fails(db):
    job = jobs.enqueue(db, "gpu", "does-not-exist", max_attempts=1)
    run_once(db, "gpu", "w")
    db.refresh(job)
    assert job.status == JobStatus.FAILED and "no handler" in job.error


def test_stale_running_job_is_requeued(db):
    job = jobs.enqueue(db, "gpu", "noop", {"seconds": 0})
    jobs.claim(db, "gpu", "dead-worker")
    job.heartbeat_at = utcnow() - timedelta(hours=1)
    db.commit()

    assert jobs.requeue_stale(db, "gpu", stale_after_seconds=300) == 1
    db.refresh(job)
    assert job.status == JobStatus.QUEUED and job.locked_by is None


def test_fresh_running_job_is_left_alone(db):
    jobs.enqueue(db, "gpu", "noop")
    jobs.claim(db, "gpu", "alive")
    assert jobs.requeue_stale(db, "gpu", stale_after_seconds=300) == 0
