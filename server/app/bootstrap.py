"""Startup tasks run once by the api container before serving."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.models import User
from app.security import hash_password

log = logging.getLogger("bootstrap")


def ensure_admin(db: Session, cfg: Config) -> None:
    """Create the break-glass admin if missing. Its password is only
    overwritten when BOOTSTRAP_ADMIN_RESET=true (e.g. you lost it)."""
    if not cfg.bootstrap_admin_password:
        log.info("BOOTSTRAP_ADMIN_PASSWORD not set; skipping admin bootstrap")
        return
    user = db.scalar(select(User).where(User.username == cfg.bootstrap_admin_username))
    if user is None:
        db.add(
            User(
                username=cfg.bootstrap_admin_username,
                password_hash=hash_password(cfg.bootstrap_admin_password),
                is_admin=True,
            )
        )
        log.info("created admin user %r", cfg.bootstrap_admin_username)
    elif cfg.bootstrap_admin_reset:
        user.password_hash = hash_password(cfg.bootstrap_admin_password)
        user.is_admin = True
        user.is_active = True
        user.auth_version += 1
        log.warning("reset password for admin user %r", cfg.bootstrap_admin_username)
    db.commit()


def main() -> None:
    from app.config import get_config
    from app.db import get_sessionmaker

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    with get_sessionmaker()() as db:
        ensure_admin(db, get_config())


if __name__ == "__main__":
    main()
