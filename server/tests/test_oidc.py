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
    client_kwargs = {"token_endpoint_auth_method": "client_secret_basic"}

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


# --- AD / LDAP style group names ------------------------------------------------

AD_ADMINS = "CN=darkside-admins,OU=Groups,DC=corp,DC=local"


def test_name_matches_exactly_ignoring_case():
    assert oidc.group_matches("darkside-admins", {"darkside-admins"})
    assert oidc.group_matches("Darkside-Admins", {"DARKSIDE-ADMINS"})
    assert oidc.group_matches("darkside-admins", oidc.normalize_groups(["/darkside-admins"]))
    assert not oidc.group_matches("darkside-admins", {"darkside-admins-old"})


def test_name_never_matches_a_dn_with_that_cn():
    """CN is only unique per OU; a same-CN group elsewhere must not count."""
    assert not oidc.group_matches("darkside-admins", {AD_ADMINS})
    assert not oidc.group_matches(
        "darkside-admins", {"CN=darkside-admins,OU=Helpdesk,DC=corp,DC=local"}
    )


def test_full_dn_matches_only_that_dn():
    assert oidc.group_matches(AD_ADMINS, {"cn=darkside-admins, ou=Groups, dc=corp, dc=local"})
    assert not oidc.group_matches(AD_ADMINS, {"CN=darkside-admins,OU=Other,DC=corp,DC=local"})
    assert not oidc.group_matches(AD_ADMINS, {"darkside-admins"})


def test_escaped_comma_in_dn():
    dn = r"CN=Sales\, Istanbul,OU=Groups,DC=corp,DC=local"
    assert oidc.group_matches(dn, {r"cn=sales\, istanbul,ou=groups,dc=corp,dc=local"})
    assert not oidc.group_matches(dn, {r"CN=Sales\, Ankara,OU=Groups,DC=corp,DC=local"})


def test_entra_guid_matches_exactly():
    guid = "3f2a9c1e-8b7d-4e6f-9a0b-1c2d3e4f5a6b"
    assert oidc.group_matches(guid.upper(), {guid})
    assert not oidc.group_matches(guid, {"3f2a9c1e-0000-0000-0000-000000000000"})


def test_empty_configured_group_never_matches():
    assert not oidc.group_matches("  ", {"anything"})


def test_upsert_with_ad_memberof_dns(db, cfg, monkeypatch):
    monkeypatch.setattr(cfg, "oidc_groups_claim", "memberOf")
    monkeypatch.setattr(cfg, "oidc_required_group", "CN=darkside-users,OU=Groups,DC=corp,DC=local")
    monkeypatch.setattr(cfg, "oidc_admin_group", AD_ADMINS)
    member_of = ["CN=darkside-users,OU=Groups,DC=corp,DC=local", AD_ADMINS]
    user = oidc.upsert_user(
        db, {"iss": ISS, "sub": "s", "preferred_username": "u", "memberOf": member_of}, cfg
    ).user
    assert user is not None and user.is_admin
    refused = oidc.upsert_user(
        db,
        {
            "iss": ISS,
            "sub": "t",
            "preferred_username": "v",
            "memberOf": ["CN=other,OU=Groups,DC=corp,DC=local"],
        },
        cfg,
    )
    assert refused.error == "not_allowed"


# --- client_secret_basic vs client_secret_post ------------------------------------


def test_pick_token_auth_method():
    pick = oidc.pick_token_auth_method
    assert pick({}) == "client_secret_basic"  # spec default
    assert pick({"token_endpoint_auth_methods_supported": ["client_secret_post"]}) == (
        "client_secret_post"
    )
    both = {"token_endpoint_auth_methods_supported": ["client_secret_post", "client_secret_basic"]}
    assert pick(both) == "client_secret_basic"


async def _token_request(method: str) -> httpx.Request:
    from authlib.integrations.httpx_client import AsyncOAuth2Client

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["request"] = request
        return httpx.Response(200, json={"access_token": "a", "token_type": "Bearer"})

    async with AsyncOAuth2Client(
        "darkside",
        "s3cret",
        token_endpoint_auth_method=method,
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.fetch_token(
            "https://idp.example/token", grant_type="authorization_code", code="c"
        )
    return seen["request"]


def test_secret_sent_where_configured():
    import asyncio

    basic = asyncio.run(_token_request("client_secret_basic"))
    assert basic.headers.get("authorization", "").startswith("Basic ")
    assert b"client_secret" not in basic.content

    post = asyncio.run(_token_request("client_secret_post"))
    assert "authorization" not in post.headers
    assert b"client_secret=s3cret" in post.content


def test_explicit_setting_and_auto_resolution(cfg, monkeypatch):
    import asyncio

    monkeypatch.setattr(cfg, "oidc_issuer", ISS)
    monkeypatch.setattr(cfg, "oidc_client_id", "darkside")
    monkeypatch.setattr(cfg, "oidc_token_auth_method", "client_secret_post")
    oidc.get_oauth.cache_clear()
    try:
        assert oidc.get_oauth().idp.client_kwargs["token_endpoint_auth_method"] == (
            "client_secret_post"
        )
        monkeypatch.setattr(cfg, "oidc_token_auth_method", "auto")
        oidc.get_oauth.cache_clear()
        idp = oidc.get_oauth().idp

        async def fake_metadata():
            return {"token_endpoint_auth_methods_supported": ["client_secret_post"]}

        monkeypatch.setattr(idp, "load_server_metadata", fake_metadata)
        asyncio.run(oidc.prepare_client())
        assert idp.client_kwargs["token_endpoint_auth_method"] == "client_secret_post"
    finally:
        oidc.get_oauth.cache_clear()
