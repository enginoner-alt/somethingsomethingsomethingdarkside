from app.models import Job, User
from app.worker import run_once
from tests.conftest import csrf_from, login


def test_admin_requires_admin(client, regular_user):
    login(client, "alice", "alice-password-1")
    assert client.get("/admin").status_code == 403


def test_create_user_and_login(client, admin, db):
    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin").text)
    res = client.post(
        "/admin/users",
        data={"username": "bob", "password": "bob-password-1", "csrf": token},
        follow_redirects=False,
    )
    assert res.headers["location"] == "/admin"
    bob = db.query(User).filter_by(username="bob").one()
    assert not bob.is_admin

    other = client.__class__(client.app)
    assert login(other, "bob", "bob-password-1").status_code == 303


def test_create_user_rejects_short_password_and_duplicates(client, admin):
    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin").text)
    short = client.post(
        "/admin/users",
        data={"username": "bob", "password": "short", "csrf": token},
        follow_redirects=False,
    )
    assert "error=invalid_user" in short.headers["location"]
    dup = client.post(
        "/admin/users",
        data={"username": "admin", "password": "long-enough-pw", "csrf": token},
        follow_redirects=False,
    )
    assert "error=user_exists" in dup.headers["location"]


def test_admin_cannot_disable_self(client, admin, db):
    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin").text)
    res = client.post(
        f"/admin/users/{admin.id}/toggle-active", data={"csrf": token}, follow_redirects=False
    )
    assert "error=self_disable" in res.headers["location"]
    db.refresh(admin)
    assert admin.is_active


def test_test_job_round_trip(client, admin, db):
    """M0 acceptance: admin queues a test job, a worker picks it up, it's done."""
    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin").text)
    client.post("/admin/jobs/test", data={"queue": "gpu", "csrf": token})

    [job] = client.get("/admin/jobs.json").json()
    assert job["status"] == "queued" and job["queue"] == "gpu"

    job_row = db.get(Job, job["id"])
    job_row.payload = {"seconds": 0}  # don't make the test sleep
    db.commit()

    assert run_once(db, "gpu", "test-worker") is True
    [job] = client.get("/admin/jobs.json").json()
    assert job["status"] == "done"
    assert job["result"]["worker"] == "test-worker"
