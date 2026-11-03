"""Tests run against a real Postgres (SKIP LOCKED etc. matter).

    TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/db pytest

The database is wiped and migrated with Alembic once per session, then
tables are truncated between tests.
"""

import os
import re
from pathlib import Path

import pytest

os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://mrec:mrec@localhost/mrec_test"
)
os.environ["SECRET_KEY"] = "test-secret-key-" + "x" * 32
os.environ["SESSION_COOKIE_SECURE"] = "false"
os.environ["BOOTSTRAP_ADMIN_USERNAME"] = "admin"
os.environ["BOOTSTRAP_ADMIN_PASSWORD"] = "admin-password-123"

from alembic import command  # noqa: E402
from alembic.config import Config as AlembicConfig  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import text  # noqa: E402

from app.bootstrap import ensure_admin  # noqa: E402
from app.config import get_config  # noqa: E402
from app.db import get_engine, get_sessionmaker  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import User  # noqa: E402
from app.security import hash_password  # noqa: E402

SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    with get_engine().begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    cfg = AlembicConfig(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture(autouse=True)
def clean_tables():
    with get_engine().begin() as conn:
        conn.execute(text("TRUNCATE jobs, login_failures, users RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
def db():
    with get_sessionmaker()() as session:
        yield session


@pytest.fixture
def admin(db):
    ensure_admin(db, get_config())
    return db.query(User).filter_by(username="admin").one()


@pytest.fixture
def regular_user(db):
    user = User(username="alice", password_hash=hash_password("alice-password-1"))
    db.add(user)
    db.commit()
    return user


@pytest.fixture
def client():
    return TestClient(create_app())


def csrf_from(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "no csrf token in page"
    return match.group(1)


def login(client: TestClient, username: str, password: str):
    token = csrf_from(client.get("/login").text)
    return client.post(
        "/login",
        data={"username": username, "password": password, "csrf": token},
        follow_redirects=False,
    )


@pytest.fixture
def cfg(monkeypatch):
    """The live (cached) config; use monkeypatch.setattr(cfg, ...) to tweak it."""
    return get_config()
