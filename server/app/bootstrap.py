"""Startup tasks run once by the api container before serving."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Config
from app.models import AuthSource, User
from app.security import hash_password, verify_password

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


def ensure_test_account(db: Session, cfg: Config) -> None:
    """Create the local test account from TEST_ACCOUNT_PASSWORD if missing.

    - Never touches is_active: if an admin disabled it, it stays disabled.
    - If the password in .env changed, the account gets the new one (and its
      open sessions end).
    - Never takes over an existing non-test account with the same name.
    """
    if not cfg.test_account_password:
        return
    name = cfg.test_account_username.strip()
    if name == cfg.bootstrap_admin_username:
        log.error("TEST_ACCOUNT_USERNAME must differ from the admin username; skipping")
        return
    user = db.scalar(select(User).where(User.username == name))
    if user is None:
        db.add(
            User(
                username=name,
                password_hash=hash_password(cfg.test_account_password),
                is_admin=False,
            )
        )
        log.info("created local test account %r", name)
    elif user.auth_source != AuthSource.LOCAL or user.is_admin:
        log.error("user %r exists and isn't a local non-admin account; not touching it", name)
        return
    elif not verify_password(user.password_hash, cfg.test_account_password):
        user.password_hash = hash_password(cfg.test_account_password)
        user.auth_version += 1
        log.info("updated password of local test account %r", name)
    db.commit()


def main() -> None:
    from app.config import get_config
    from app.db import get_sessionmaker

    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    with get_sessionmaker()() as db:
        ensure_admin(db, get_config())
        ensure_test_account(db, get_config())


if __name__ == "__main__":
    main()
