import httpx

from app import oidc
from app.models import AuthSource, User
from app.security import hash_password

ISS = "https://idp.example/realms/darkside"


def claims(sub="sub-1", username="jdoe", groups=("/darkside-users",), **extra):
    return {"iss": ISS, "sub": sub, "preferred_username": username, "groups": list(groups), **extra}


def test_normalize_groups():
    assert oidc.normalize_groups(["/a", "b", " /c/d ", ""]) == {"a", "b", "c/d"}
    assert oidc.normalize_groups("solo") == {"solo"}
    assert oidc.normalize_groups(None) == set()


def test_creates_user_on_first_login(db, cfg):
    result = oidc.upsert_user(db, claims(name="Jane Doe"), cfg)
    user = result.user
    assert user and user.username == "jdoe" and user.display_name == "Jane Doe"
    assert user.auth_source == AuthSource.OIDC and user.password_hash is None
    assert (user.oidc_issuer, user.oidc_subject) == (ISS, "sub-1")


def test_same_subject_reuses_user_and_follows_rename(db, cfg):
    first = oidc.upsert_user(db, claims(), cfg).user
    again = oidc.upsert_user(db, claims(username="jane.doe"), cfg).user
    assert again.id == first.id and again.username == "jane.doe"
    assert db.query(User).count() == 1


def test_required_group(db, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "oidc_required_group", "darkside-users")
    assert oidc.upsert_user(db, claims(groups=["/other"]), cfg).error == "not_allowed"
    assert oidc.upsert_user(db, claims(), cfg).user is not None


def test_admin_group_granted_and_revoked(db, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "oidc_admin_group", "/darkside-admins")
    user = oidc.upsert_user(db, claims(groups=["/darkside-admins"]), cfg).user
    assert user.is_admin
    user = oidc.upsert_user(db, claims(groups=["/darkside-users"]), cfg).user
    assert not user.is_admin


def test_no_admin_group_configured_leaves_role_alone(db, cfg):
    user = oidc.upsert_user(db, claims(), cfg).user
    user.is_admin = True
    db.commit()
    assert oidc.upsert_user(db, claims(), cfg).user.is_admin


def test_never_takes_over_existing_username(db, cfg):
    db.add(User(username="admin", password_hash=hash_password("x" * 12), is_admin=True))
    db.commit()
    result = oidc.upsert_user(db, claims(username="admin", sub="attacker"), cfg)
    assert result.error == "conflict" and result.user is None


def test_rename_into_taken_username_is_ignored(db, cfg):
    db.add(User(username="taken", password_hash=hash_password("x" * 12)))
    db.commit()
    oidc.upsert_user(db, claims(), cfg)
    user = oidc.upsert_user(db, claims(username="taken"), cfg).user
    assert user.username == "jdoe"


def test_disabled_in_app_is_refused(db, cfg):
    user = oidc.upsert_user(db, claims(), cfg).user
    user.is_active = False
    db.commit()
    assert oidc.upsert_user(db, claims(), cfg).error == "disabled"


def test_missing_subject_is_refused(db, cfg):
    assert oidc.upsert_user(db, {"iss": ISS}, cfg).error == "sso_failed"


def test_oidc_routes_404_when_not_configured(client):
    assert client.get("/auth/oidc/login").status_code == 404
    assert client.get("/auth/oidc/callback").status_code == 404
    assert "Sign in with" not in client.get("/login").text


def test_login_page_shows_sso_when_configured(client, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "oidc_issuer", ISS)
    monkeypatch.setattr(cfg, "oidc_client_id", "darkside")
    monkeypatch.setattr(cfg, "oidc_button_label", "Company SSO")
    page = client.get("/login").text
    assert "Sign in with Company SSO" in page and 'name="password"' in page

    monkeypatch.setattr(cfg, "local_login_mode", "break_glass_only")
    page = client.get("/login").text
    assert "Emergency administrator login" in page


class _SlowIdp:
    async def authorize_access_token(self, request):
        raise httpx.ReadTimeout("IdP took too long")


class _FakeOAuth:
    idp = _SlowIdp()


def test_callback_idp_timeout_shows_message_not_500(client, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "oidc_issuer", ISS)
    monkeypatch.setattr(cfg, "oidc_client_id", "darkside")
    monkeypatch.setattr(oidc, "get_oauth", lambda: _FakeOAuth())
    res = client.get("/auth/oidc/callback?code=x&state=y")
    assert res.status_code == 503
    assert "sign-in service is unreachable" in res.text


def test_oidc_client_uses_extra_cas_and_longer_timeout(cfg, monkeypatch):
    monkeypatch.setattr(cfg, "oidc_issuer", ISS)
    monkeypatch.setattr(cfg, "oidc_client_id", "darkside")
    oidc.get_oauth.cache_clear()
    try:
        kwargs = oidc.get_oauth().idp.client_kwargs
        assert kwargs["timeout"] >= 15 and kwargs["verify"] is not None
    finally:
        oidc.get_oauth.cache_clear()
