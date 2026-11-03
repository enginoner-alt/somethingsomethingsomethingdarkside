import time

from fastapi.testclient import TestClient

from app import web
from tests.conftest import csrf_from, login


def _login_from(client: TestClient, ip: str, username: str, password: str):
    """TestClient always reports 'testclient' as the peer; vary it per call."""
    client.app.state  # noqa: B018  (ensure app built)
    token = csrf_from(client.get("/login").text)
    with TestClient(client.app, client=(ip, 50000), cookies=client.cookies) as c:
        return c.post(
            "/login",
            data={"username": username, "password": password, "csrf": token},
            follow_redirects=False,
        )


def test_throttle_per_user_and_ip(client, admin, cfg):
    for _ in range(cfg.login_max_failures_per_user_ip):
        assert login(client, "admin", "wrong").status_code == 401
    # Locked now, even with the right password.
    res = login(client, "admin", "admin-password-123")
    assert res.status_code == 429
    assert "Too many failed attempts" in res.text


def test_throttle_does_not_lock_admin_out_from_other_ip(client, admin, cfg):
    for _ in range(cfg.login_max_failures_per_user_ip):
        _login_from(client, "203.0.113.9", "admin", "wrong")
    assert _login_from(client, "203.0.113.9", "admin", "admin-password-123").status_code == 429
    assert _login_from(client, "198.51.100.7", "admin", "admin-password-123").status_code == 303


def test_throttle_per_ip_across_usernames(client, admin, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "login_max_failures_per_ip", 6)
    for i in range(6):
        assert login(client, f"user{i}", "wrong").status_code == 401
    assert login(client, "admin", "admin-password-123").status_code == 429


def test_success_clears_failures(client, admin, cfg):
    for _ in range(cfg.login_max_failures_per_user_ip - 1):
        login(client, "admin", "wrong")
    assert login(client, "admin", "admin-password-123").status_code == 303
    client.cookies.clear()
    for _ in range(cfg.login_max_failures_per_user_ip - 1):
        assert login(client, "admin", "wrong").status_code == 401


def test_break_glass_only_mode(client, admin, regular_user, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "local_login_mode", "break_glass_only")
    res = login(client, "alice", "alice-password-1")
    assert res.status_code == 401 and "Wrong username or password" in res.text
    assert login(client, "admin", "admin-password-123").status_code == 303


def test_idle_timeout(client, admin, cfg, monkeypatch):
    login(client, "admin", "admin-password-123")
    assert client.get("/", follow_redirects=False).status_code == 200
    later = time.time() + cfg.session_idle_timeout_seconds + 5
    monkeypatch.setattr(web.time, "time", lambda: later)
    assert client.get("/", follow_redirects=False).status_code == 303


def test_activity_keeps_session_alive(client, admin, cfg, monkeypatch):
    login(client, "admin", "admin-password-123")
    step = cfg.session_idle_timeout_seconds * 0.6
    now = time.time()
    for i in range(1, 4):  # 3 requests, each 60% of the timeout apart
        monkeypatch.setattr(web.time, "time", lambda t=now + i * step: t)
        assert client.get("/", follow_redirects=False).status_code == 200


def test_disabling_user_revokes_session_immediately(client, admin, regular_user, db):
    alice = TestClient(client.app)
    login(alice, "alice", "alice-password-1")
    assert alice.get("/", follow_redirects=False).status_code == 200

    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin").text)
    url = f"/admin/users/{regular_user.id}/toggle-active"
    client.post(url, data={"csrf": token})  # disable
    assert alice.get("/", follow_redirects=False).status_code == 303
    client.post(url, data={"csrf": token})  # re-enable
    # The old session stays dead; she has to log in again.
    assert alice.get("/", follow_redirects=False).status_code == 303
    assert login(alice, "alice", "alice-password-1").status_code == 303


def test_failed_login_does_not_reveal_unknown_vs_wrong(client, admin):
    a = login(client, "admin", "wrong-password")
    b = login(client, "nobody-here", "wrong-password")
    assert a.status_code == b.status_code == 401
    assert "Wrong username or password" in a.text and "Wrong username or password" in b.text
