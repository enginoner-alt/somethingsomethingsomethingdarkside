"""Brute-force protection for local (password) logins.

Stored in Postgres so it survives restarts and works with several api
processes. Two limits within a sliding window:

- per (username, IP): stops guessing one account from one place, without
  letting a stranger lock the real admin out from elsewhere;
- per IP: stops spraying many usernames from one place.

While a limit is hit the password is not even checked.
"""

from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.config import Config
from app.db import utcnow
from app.models import LoginFailure


def _norm(username: str) -> str:
    return username.strip().lower()[:150]


def is_throttled(db: Session, username: str, ip: str, cfg: Config) -> bool:
    since = utcnow() - timedelta(seconds=cfg.login_failure_window_seconds)
    recent = select(func.count()).select_from(LoginFailure).where(LoginFailure.created_at > since)
    per_pair = db.scalar(
        recent.where(LoginFailure.username == _norm(username), LoginFailure.ip == ip)
    )
    if per_pair >= cfg.login_max_failures_per_user_ip:
        return True
    per_ip = db.scalar(recent.where(LoginFailure.ip == ip))
    return per_ip >= cfg.login_max_failures_per_ip


def record_failure(db: Session, username: str, ip: str, cfg: Config) -> None:
    cutoff = utcnow() - timedelta(seconds=cfg.login_failure_window_seconds)
    db.execute(delete(LoginFailure).where(LoginFailure.created_at < cutoff))
    db.add(LoginFailure(username=_norm(username), ip=ip))
    db.commit()


def clear_failures(db: Session, username: str, ip: str) -> None:
    db.execute(
        delete(LoginFailure).where(LoginFailure.username == _norm(username), LoginFailure.ip == ip)
    )
    db.commit()
