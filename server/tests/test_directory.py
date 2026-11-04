"""Direct AD login: policy, account handling, admin page. A fake directory
stands in for AD here; the real thing is tested against Samba AD
(dev/ad-test/)."""

import base64
import uuid

import pytest

from app import directory, login_policy
from app.login_policy import Policy
from app.models import User
from app.security import hash_password
from app.settings_store import DirectorySettings, get_directory, save_directory
from tests.conftest import csrf_from, login

GUID = "f22cf220-5b96-4e0e-a309-a556465677e8"


# --- pure helpers ------------------------------------------------------------------


def test_normalize_guid_forms():
    raw = uuid.UUID(GUID).bytes_le
    assert directory.normalize_guid("{F22CF220-5B96-4E0E-A309-A556465677E8}") == GUID
    assert directory.normalize_guid(GUID) == GUID
    assert directory.normalize_guid(raw) == GUID
    assert directory.normalize_guid(base64.b64encode(raw).decode()) == GUID  # Entra
    assert directory.normalize_guid("not-a-guid") is None
    assert directory.normalize_guid(None) is None


def test_config_problems(secrets_dir):
    s = DirectorySettings(enabled=True, servers=["ldap://dc01:389"], bind_dn="x", search_base="y")
    problems = directory.config_problems(s)
    assert any("plain LDAP is refused" in p for p in problems)
    assert any("ldap_bind_password" in p for p in problems)
    (secrets_dir / "ldap_bind_password").write_text("pw\n")
    s.start_tls = True
    assert directory.config_problems(s) == []


def test_empty_password_never_sent():
    d = directory.Directory(DirectorySettings(servers=["ldaps://dc01.invalid:636"]))
    assert d.check_password("CN=x", "") is False  # no network attempted


@pytest.mark.parametrize(
    "oidc_cfg, oidc_ok, ldap_cfg, ldap_ok, preferred, allowed",
    [
        (True, True, True, True, "oidc", False),  # both work, SSO preferred -> SSO
        (True, True, True, True, "ldap", True),  # both work, AD preferred -> AD
        (True, False, True, True, "oidc", True),  # SSO down -> AD
        (True, True, True, False, "oidc", False),  # AD down -> SSO
        (True, True, True, False, "ldap", False),  # AD preferred but down -> SSO
        (False, False, True, True, "oidc", True),  # no SSO at all -> AD
        (True, False, True, False, "oidc", False),  # nothing works -> local only
        (False, False, False, False, "oidc", False),  # nothing configured
    ],
)
def test_policy_matrix(oidc_cfg, oidc_ok, ldap_cfg, ldap_ok, preferred, allowed):
    p = Policy(oidc_cfg, oidc_ok, ldap_cfg, ldap_ok, preferred)
    assert p.ad_passwords_allowed is allowed


# --- login route with a fake AD -------------------------------------------------------


class FakeDirectory:
    users: dict = {}
    unavailable = False

    def __init__(self, settings):
        self.settings = settings

    def authenticate(self, username, password):
        if FakeDirectory.unavailable:
            raise directory.DirectoryUnavailable("DC down")
        entry = FakeDirectory.users.get(username)
        if not entry or entry["password"] != password or entry.get("disabled"):
            return None
        return directory.DirectoryUser(
            guid=entry["guid"],
            username=username,
            dn=f"CN={username},CN=Users,DC=corp,DC=test",
            display_name=entry.get("name"),
            disabled=False,
            groups=set(entry["groups"]),
        )


@pytest.fixture
def fake_ad(db, monkeypatch):
    FakeDirectory.users = {
        "jdoe": {
            "password": "pw-jdoe",
            "guid": GUID,
            "name": "Jane Doe",
            "groups": ["darkside-users", "darkside-admins"],
        },
        "asmith": {
            "password": "pw-asmith",
            "guid": str(uuid.uuid4()),
            "groups": ["darkside-users"],
        },
        "mallory": {
            "password": "pw-mallory",
            "guid": str(uuid.uuid4()),
            "groups": ["helpdesk-admins", "CN=darkside-admins,OU=Helpdesk,DC=corp,DC=test"],
        },
    }
    FakeDirectory.unavailable = False
    monkeypatch.setattr(directory, "Directory", FakeDirectory)
    save_directory(
        db,
        DirectorySettings(
            enabled=True,
            servers=["ldaps://dc01.corp.test:636"],
            bind_dn="CN=svc",
            search_base="DC=corp",
            required_group="darkside-users",
            admin_group="darkside-admins",
        ),
    )
    state = {"policy": Policy(False, False, True, True, "oidc")}
    monkeypatch.setattr(login_policy, "current", lambda db: state["policy"])
    return state


def test_ad_login_creates_linked_account(client, db, fake_ad):
    assert login(client, "jdoe", "pw-jdoe").status_code == 303
    user = db.query(User).filter_by(username="jdoe").one()
    assert user.auth_source == "ldap" and user.ad_guid == GUID and user.password_hash is None
    assert user.is_admin and user.display_name == "Jane Doe"
    assert client.get("/admin").status_code == 200


def test_ad_regular_user_and_required_group(client, db, fake_ad):
    assert login(client, "asmith", "pw-asmith").status_code == 303
    assert client.get("/admin").status_code == 403
    other = client.__class__(client.app)
    res = login(other, "mallory", "pw-mallory")  # decoy group only: not in darkside-users
    assert res.status_code == 403 and "allowed to use this app" in res.text


def test_ad_wrong_password_and_unknown_user(client, fake_ad):
    assert login(client, "jdoe", "wrong").status_code == 401
    assert login(client, "nobody", "x").status_code == 401


def test_ad_unavailable_shows_message(client, fake_ad):
    FakeDirectory.unavailable = True
    res = login(client, "jdoe", "pw-jdoe")
    assert res.status_code == 503 and "directory (AD) is unreachable" in res.text


def test_sso_working_and_preferred_refuses_ad_password(client, fake_ad):
    fake_ad["policy"] = Policy(True, True, True, True, "oidc")
    res = login(client, "jdoe", "pw-jdoe")
    assert res.status_code == 401 and "single sign-on button" in res.text


def test_sso_down_accepts_ad_password(client, fake_ad):
    fake_ad["policy"] = Policy(True, False, True, True, "oidc")
    page = client.get("/login").text
    assert "Single sign-on is unavailable right now" in page
    assert "/auth/oidc/login" not in page  # dead SSO button hidden
    assert login(client, "jdoe", "pw-jdoe").status_code == 303


def test_local_accounts_take_precedence_and_still_work(client, db, admin, fake_ad):
    FakeDirectory.users["admin"] = {"password": "ad-pw", "guid": str(uuid.uuid4()), "groups": []}
    assert login(client, "admin", "ad-pw").status_code == 401  # AD can't hijack a local name
    assert login(client, "admin", "admin-password-123").status_code == 303


def test_break_glass_only_still_allows_ad_when_sso_down(
    client, db, admin, cfg, monkeypatch, fake_ad
):
    monkeypatch.setattr(cfg, "local_login_mode", "break_glass_only")
    db.add(User(username="localuser", password_hash=hash_password("local-password-1")))
    db.commit()
    fake_ad["policy"] = Policy(True, False, True, True, "oidc")
    assert login(client, "localuser", "local-password-1").status_code == 401
    assert login(client, "jdoe", "pw-jdoe").status_code == 303


def test_ad_username_taken_by_other_account_is_refused(client, db, fake_ad):
    db.add(User(username="jdoe", auth_source="oidc", oidc_issuer="i", oidc_subject="s"))
    db.commit()
    res = login(client, "jdoe", "pw-jdoe")
    assert res.status_code == 403 and "already exists" in res.text


def test_disabled_in_app_blocks_ad_login(client, db, fake_ad):
    login(client, "asmith", "pw-asmith")
    u = db.query(User).filter_by(username="asmith").one()
    u.is_active = False
    db.commit()
    other = client.__class__(client.app)
    assert login(other, "asmith", "pw-asmith").status_code == 403


def test_sso_and_ad_logins_are_one_account(client, db, cfg, monkeypatch, fake_ad):
    from app import oidc

    assert login(client, "jdoe", "pw-jdoe").status_code == 303  # first via AD
    monkeypatch.setattr(cfg, "oidc_ad_guid_claim", "objectGUID")
    claims = {
        "iss": "https://idp",
        "sub": "kc-123",
        "preferred_username": "jdoe",
        "groups": [],
        "objectGUID": "{" + GUID.upper() + "}",
    }
    result = oidc.upsert_user(db, claims, cfg)
    assert result.user is not None
    assert db.query(User).count() == 1
    assert (result.user.oidc_subject, result.user.ad_guid) == ("kc-123", GUID)


# --- admin page ------------------------------------------------------------------------


def test_admin_directory_page_and_save(client, db, admin, secrets_dir):
    login(client, "admin", "admin-password-123")
    page = client.get("/admin/directory").text
    assert "not set" in page  # secret missing
    (secrets_dir / "ldap_bind_password").write_text("super-secret-value")
    page = client.get("/admin/directory").text
    assert "super-secret-value" not in page and "fingerprint" in page
    res = client.post(
        "/admin/directory",
        data={
            "csrf": csrf_from(page),
            "enabled": "on",
            "servers": "ldaps://dc01.corp.test:636\nldaps://dc02.corp.test:636",
            "bind_dn": "CN=svc",
            "search_base": "DC=corp",
            "required_group": "darkside-users",
            "nested_groups": "on",
            "preferred_method": "ldap",
        },
        follow_redirects=False,
    )
    assert res.headers["location"] == "/admin/directory?saved=1"
    s = get_directory(db)
    assert s.enabled and s.servers == ["ldaps://dc01.corp.test:636", "ldaps://dc02.corp.test:636"]
    assert s.nested_groups and not s.start_tls


def test_admin_directory_rejects_bad_url(client, db, admin):
    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin/directory").text)
    res = client.post("/admin/directory", data={"csrf": token, "servers": "dc01.corp.test"})
    assert "must start with ldaps://" in res.text


def test_verify_buttons_report_problems(client, db, admin):
    login(client, "admin", "admin-password-123")
    token = csrf_from(client.get("/admin/directory").text)
    sso = client.post("/admin/directory/verify-sso", data={"csrf": token}).text
    assert "OIDC_ISSUER / OIDC_CLIENT_ID not set" in sso
    save_directory(db, DirectorySettings(enabled=True))
    ad = client.post("/admin/directory/verify-ad", data={"csrf": token}).text
    assert "no server configured" in ad


def test_directory_page_admin_only(client, regular_user):
    login(client, "alice", "alice-password-1")
    assert client.get("/admin/directory").status_code == 403
