from app.bootstrap import ensure_admin
from app.config import Config
from app.models import User
from app.security import verify_password
from tests.conftest import csrf_from, login


def test_healthz(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_home_redirects_to_login(client):
    res = client.get("/", follow_redirects=False)
    assert res.status_code == 303
    assert res.headers["location"].startswith("/login")


def test_login_success_and_logout(client, admin):
    res = login(client, "admin", "admin-password-123")
    assert res.status_code == 303 and res.headers["location"] == "/"
    home = client.get("/")
    assert home.status_code == 200 and "admin" in home.text

    res = client.post("/logout", data={"csrf": csrf_from(home.text)}, follow_redirects=False)
    assert res.status_code == 303
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_wrong_password(client, admin):
    res = login(client, "admin", "nope")
    assert res.status_code == 401
    assert client.get("/", follow_redirects=False).status_code == 303


def test_login_unknown_user(client):
    assert login(client, "ghost", "whatever").status_code == 401


def test_disabled_user_cannot_login(client, db, regular_user):
    regular_user.is_active = False
    db.commit()
    assert login(client, "alice", "alice-password-1").status_code == 401


def test_login_requires_csrf(client, admin):
    client.get("/login")
    res = client.post("/login", data={"username": "admin", "password": "admin-password-123"})
    assert res.status_code == 400


def test_open_redirect_blocked(client, admin):
    token = csrf_from(client.get("/login").text)
    res = client.post(
        "/login",
        data={
            "username": "admin",
            "password": "admin-password-123",
            "csrf": token,
            "next": "//evil.example",
        },
        follow_redirects=False,
    )
    assert res.headers["location"] == "/"


def test_bootstrap_is_idempotent_and_reset(db):
    cfg = Config(
        secret_key="k" * 32,
        bootstrap_admin_username="root",
        bootstrap_admin_password="first-password",
    )
    ensure_admin(db, cfg)
    ensure_admin(db, cfg.model_copy(update={"bootstrap_admin_password": "second-password"}))
    user = db.query(User).filter_by(username="root").one()
    assert verify_password(user.password_hash, "first-password")

    reset = cfg.model_copy(
        update={"bootstrap_admin_password": "second-password", "bootstrap_admin_reset": True}
    )
    ensure_admin(db, reset)
    db.refresh(user)
    assert verify_password(user.password_hash, "second-password")
