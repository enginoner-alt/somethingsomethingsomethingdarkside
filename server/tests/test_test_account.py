from app.bootstrap import ensure_test_account
from app.models import User
from app.security import hash_password, verify_password
from tests.conftest import csrf_from, login


def _with_test_account(cfg, monkeypatch, password="test-password-1"):
    monkeypatch.setattr(cfg, "test_account_username", "test")
    monkeypatch.setattr(cfg, "test_account_password", password)


def test_not_created_without_password(db, cfg):
    ensure_test_account(db, cfg)
    assert db.query(User).filter_by(username="test").count() == 0


def test_created_as_regular_local_user(db, cfg, monkeypatch):
    _with_test_account(cfg, monkeypatch)
    ensure_test_account(db, cfg)
    user = db.query(User).filter_by(username="test").one()
    assert user.is_active and not user.is_admin and user.auth_source == "local"
    assert verify_password(user.password_hash, "test-password-1")


def test_restart_does_not_re_enable(db, cfg, monkeypatch):
    _with_test_account(cfg, monkeypatch)
    ensure_test_account(db, cfg)
    user = db.query(User).filter_by(username="test").one()
    user.is_active = False
    db.commit()
    ensure_test_account(db, cfg)  # like an api restart
    db.refresh(user)
    assert not user.is_active


def test_password_change_in_env_is_applied(db, cfg, monkeypatch):
    _with_test_account(cfg, monkeypatch)
    ensure_test_account(db, cfg)
    _with_test_account(cfg, monkeypatch, password="new-password-22")
    ensure_test_account(db, cfg)
    user = db.query(User).filter_by(username="test").one()
    assert verify_password(user.password_hash, "new-password-22")
    assert user.auth_version == 1


def test_never_takes_over_admin_or_other_accounts(db, cfg, monkeypatch):
    _with_test_account(cfg, monkeypatch)
    db.add(User(username="test", password_hash=hash_password("x" * 12), is_admin=True))
    db.commit()
    ensure_test_account(db, cfg)
    user = db.query(User).filter_by(username="test").one()
    assert user.is_admin and not verify_password(user.password_hash, "test-password-1")

    monkeypatch.setattr(cfg, "test_account_username", cfg.bootstrap_admin_username)
    ensure_test_account(db, cfg)  # same name as admin: skipped, no crash


def test_admin_can_disable_and_enable_test_account(client, admin, db, cfg, monkeypatch):
    _with_test_account(cfg, monkeypatch)
    ensure_test_account(db, cfg)
    test_user = db.query(User).filter_by(username="test").one()

    tester = client.__class__(client.app)
    assert login(tester, "test", "test-password-1").status_code == 303

    login(client, "admin", "admin-password-123")
    page = client.get("/admin").text
    assert "test account" in page  # badge in the user list
    token = csrf_from(page)
    url = f"/admin/users/{test_user.id}/toggle-active"

    client.post(url, data={"csrf": token})  # disable
    assert tester.get("/", follow_redirects=False).status_code == 303  # kicked out
    tester.cookies.clear()
    assert login(tester, "test", "test-password-1").status_code == 401  # can't log back in

    client.post(url, data={"csrf": token})  # enable
    assert login(tester, "test", "test-password-1").status_code == 303
