"""Job worker.

    python -m app.worker --queue gpu
    python -m app.worker --queue llm

One job at a time per process. While a handler runs, a background thread
keeps the job's heartbeat fresh so other workers don't think it's orphaned.
"""

import argparse
import logging
import os
import signal
import socket
import threading
import time
import traceback
from typing import Any

from sqlalchemy.orm import Session

from app import (
    chats,  # noqa: F401  (registers the chat job handler)
    jobs,
    pipeline,  # noqa: F401  (registers the M3 job handlers)
)
from app.config import get_config
from app.db import get_sessionmaker
from app.models import Job, Queue
from app.registry import HANDLERS, handler

log = logging.getLogger("worker")


@handler("noop")
def noop(db: Session, job: Job) -> dict[str, Any]:
    """Test job: sleeps, then reports which worker ran it. Used by the admin
    panel's "run test job" button to prove the queue works end to end."""
    seconds = min(float(job.payload.get("seconds", 1)), 60)
    time.sleep(seconds)
    if job.payload.get("fail"):
        raise RuntimeError("asked to fail")
    return {"ok": True, "slept": seconds, "worker": job.locked_by}


class _Heartbeat(threading.Thread):
    def __init__(self, job_id: int, worker_id: str, interval: float) -> None:
        super().__init__(daemon=True)
        self.job_id, self.worker_id, self.interval = job_id, worker_id, interval
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(self.interval):
            try:
                with get_sessionmaker()() as db:
                    jobs.heartbeat(db, self.job_id, self.worker_id)
            except Exception:  # keep beating; a blip shouldn't kill the job
                log.exception("heartbeat failed for job %s", self.job_id)


def run_once(db: Session, queue: str, worker_id: str) -> bool:
    """Claim and run at most one job. Returns True if a job was processed."""
    cfg = get_config()
    jobs.requeue_stale(db, queue, cfg.worker_stale_after_seconds)
    job = jobs.claim(db, queue, worker_id)
    if job is None:
        return False

    log.info("job %s (%s) started, attempt %s", job.id, job.type, job.attempts)
    beat = _Heartbeat(job.id, worker_id, cfg.worker_heartbeat_seconds)
    beat.start()
    try:
        fn = HANDLERS.get(job.type)
        if fn is None:
            raise LookupError(f"no handler for job type {job.type!r}")
        result = fn(db, job)
    except Exception:
        db.rollback()
        jobs.fail(db, job, traceback.format_exc(limit=5))
        log.warning("job %s failed (attempt %s/%s)", job.id, job.attempts, job.max_attempts)
    else:
        jobs.complete(db, job, result)
        log.info("job %s done", job.id)
    finally:
        beat.stop.set()
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue", choices=Queue.ALL, required=True)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    worker_id = os.environ.get("WORKER_ID") or f"{args.queue}@{socket.gethostname()}"
    cfg = get_config()

    stopping = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stopping.set())

    log.info("worker %s listening on queue %r", worker_id, args.queue)
    while not stopping.is_set():
        try:
            with get_sessionmaker()() as db:
                busy = run_once(db, args.queue, worker_id)
        except Exception:
            log.exception("worker loop error")
            busy = False
        if not busy:
            stopping.wait(cfg.worker_poll_seconds)
    log.info("worker %s stopped", worker_id)


if __name__ == "__main__":
    main()
