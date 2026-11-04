"""End-to-end test of the direct AD login against a real (Samba) AD DC.

TEST HARNESS ONLY. Needs: Samba AD seeded by seed-samba.sh and reachable as
dc01.corp.test:636 with a certificate the app trusts (EXTRA_CA_DIR); the app
on APP_URL with SECRETS_DIR containing ldap_bind_password; and a fake OIDC
discovery server that this script starts/stops itself (FAKE_IDP_PORT) so the
"SSO working / SSO down" switch can be tested. Run with the app configured as
OIDC_ISSUER=http://127.0.0.1:$FAKE_IDP_PORT/realms/test.

    python e2e_ad.py
"""

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

APP = os.environ.get("APP_URL", "http://localhost:8000")
IDP_PORT = int(os.environ.get("FAKE_IDP_PORT", "18090"))
ADMIN_PW = os.environ.get("BOOTSTRAP_ADMIN_PASSWORD", "admin-password-123")
PW = "Passw0rd-Test!"
SECRET = os.environ.get("LDAP_BIND_PASSWORD_VALUE", PW)

DC = "ldaps://dc01.corp.test:636"
DEAD_DC = "ldaps://dc02.corp.test:636"  # resolves to an address nothing answers on

failures = 0


def check(ok, label, detail=""):
    global failures
    print(f"{'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail and not ok else ""))
    if not ok:
        failures += 1


# --- fake IdP: only the discovery document --------------------------------------------


class Discovery(BaseHTTPRequestHandler):
    def do_GET(self):
        base = f"http://127.0.0.1:{IDP_PORT}/realms/test"
        body = json.dumps({
            "issuer": base,
            "authorization_endpoint": base + "/auth",
            "token_endpoint": base + "/token",
            "jwks_uri": base + "/certs",
        }).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class FakeIdp:
    def __init__(self):
        self.srv = None

    def up(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", IDP_PORT), Discovery)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def down(self):
        if self.srv:
            self.srv.shutdown()
            self.srv.server_close()
            self.srv = None


# --- app helpers -------------------------------------------------------------------------


def client():
    return httpx.Client(base_url=APP, follow_redirects=False, timeout=30)


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


def login(c, user, pw):
    token = csrf(c.get("/login").text)
    return c.post("/login", data={"username": user, "password": pw, "csrf": token})


def save_settings(admin, **overrides):
    form = {
        "enabled": "on",
        "servers": DC,
        "bind_dn": "CN=svc-darkside,CN=Users,DC=corp,DC=test",
        "search_base": "DC=corp,DC=test",
        "required_group": "darkside-users",
        "admin_group": "darkside-admins",
        "nested_groups": "on",
        "preferred_method": "oidc",
    }
    form.update(overrides)
    form = {k: v for k, v in form.items() if v is not None}
    form["csrf"] = csrf(admin.get("/admin/directory").text)
    r = admin.post("/admin/directory", data=form)
    assert r.status_code == 303, r.text[:300]


def verify_ad(admin, user="", pw=""):
    token = csrf(admin.get("/admin/directory").text)
    html = admin.post("/admin/directory/verify-ad",
                      data={"csrf": token, "test_username": user, "test_password": pw}).text
    rows = re.findall(r'badge (done|failed)">(?:OK|FAIL)</span></td>\s*<td>([^<]*)</td><td class="mono">([^<]*)</td>', html)
    return [(state == "done", label.strip(), detail.strip()) for state, label, detail in rows]


def refresh_policy(admin):
    """The policy caches health for 30 s; the Verify SSO button clears it."""
    admin.post("/admin/directory/verify-sso", data={"csrf": csrf(admin.get("/admin/directory").text)})


def main():
    idp = FakeIdp()
    idp.up()
    admin = client()
    assert login(admin, "admin", ADMIN_PW).status_code == 303, "admin login failed"

    # --- admin page and Verify AD ---------------------------------------------------------
    page = admin.get("/admin/directory").text
    check("fingerprint" in page and SECRET not in page, "secret shown as set + fingerprint, value never shown")

    save_settings(admin)
    steps = verify_ad(admin)
    check(steps and all(ok for ok, _, _ in steps), "Verify AD: settings, DC (LDAPS cert verified), service bind, search base",
          str([s for s in steps if not s[0]]))

    steps = dict((label, (ok, detail)) for ok, label, detail in verify_ad(admin, "nina", PW))
    groups = steps.get("Groups", (False, ""))[1]
    check("darkside-users" in groups and "eng" in groups, "Verify AD nina: nested group darkside-users via eng", groups)
    check(steps.get("Required group darkside-users", (False,))[0], "Verify AD nina: required group matched")
    check(steps.get("Test password", (False,))[0], "Verify AD nina: test password accepted by AD")

    steps = dict((label, (ok, detail)) for ok, label, detail in verify_ad(admin, "mallory"))
    check(not steps.get("Required group darkside-users", (True,))[0], "Verify AD mallory: not in required group")
    check(steps.get("Admin group darkside-admins", (None, ""))[1] == "not admin",
          "Verify AD mallory: decoy CN=darkside-admins in OU=Helpdesk does NOT count as admin")

    steps = dict((label, (ok, detail)) for ok, label, detail in verify_ad(admin, "dave"))
    check(not steps.get("Account enabled", (True,))[0], "Verify AD dave: reported as disabled in AD")

    # --- SSO works + preferred: AD passwords refused ----------------------------------------
    refresh_policy(admin)
    c = client()
    page = c.get("/login").text
    check("/auth/oidc/login" in page, "SSO up: login page shows the SSO button")
    r = login(c, "jdoe", PW)
    check(r.status_code == 401 and "single sign-on button" in r.text, "SSO up + preferred: AD password refused, sent to SSO")

    # --- SSO down: AD takes over --------------------------------------------------------------
    idp.down()
    refresh_policy(admin)
    c = client()
    page = c.get("/login").text
    check("Single sign-on is unavailable right now" in page and "/auth/oidc/login" not in page,
          "SSO down: notice shown, dead SSO button hidden")
    r = login(c, "jdoe", PW)
    check(r.status_code == 303 and c.get("/admin").status_code == 200, "SSO down: jdoe logs in with AD password, is admin")
    c = client()
    check(login(c, "asmith", PW).status_code == 303 and c.get("/admin").status_code == 403, "asmith: AD login, not admin")
    c = client()
    check(login(c, "nina", PW).status_code == 303, "nina: allowed through nested group")
    r = login(client(), "dave", PW)
    check(r.status_code == 401, "dave: disabled in AD, refused")
    r = login(client(), "mallory", PW)
    check(r.status_code == 403, "mallory: only in decoy group, refused")
    check(login(client(), "asmith", "wrong-password").status_code == 401, "wrong password refused")
    check(login(client(), "asmith", "").status_code in (401, 422), "empty password refused (no unauthenticated bind)")
    check(login(client(), "*", PW).status_code == 401, "LDAP filter characters in username don't match anyone")

    # --- preferred = AD while SSO is up ----------------------------------------------------------
    idp.up()
    save_settings(admin, preferred_method="ldap")
    refresh_policy(admin)
    c = client()
    page = c.get("/login").text
    check(login(c, "asmith", PW).status_code == 303 and "/auth/oidc/login" in page,
          "AD preferred + SSO up: AD password accepted, SSO button still shown")

    # --- DC failover --------------------------------------------------------------------------------
    save_settings(admin, servers=f"{DEAD_DC}\n{DC}", preferred_method="ldap")
    steps = verify_ad(admin)
    by = {label: ok for ok, label, _ in steps}
    check(by.get(DEAD_DC) is False and by.get(DC) is True, "Verify AD: dead DC reported, working DC OK")
    check(login(client(), "asmith", PW).status_code == 303, "login still works with the first DC down (failover)")

    # --- plain LDAP refused -------------------------------------------------------------------------
    save_settings(admin, servers="ldap://dc01.corp.test:389", preferred_method="ldap")
    steps = verify_ad(admin)
    check(steps and not steps[0][0] and "plain LDAP is refused" in steps[0][2], "plain ldap:// without StartTLS refused")

    # --- StartTLS on 389 works too -----------------------------------------------------------------
    save_settings(admin, servers="ldap://dc01.corp.test:389", start_tls="on", preferred_method="ldap")
    steps = verify_ad(admin)
    check(steps and all(ok for ok, _, _ in steps), "StartTLS on 389: verified and bound",
          str([s for s in steps if not s[0]]))
    check(login(client(), "asmith", PW).status_code == 303, "login over StartTLS")

    # --- disabled in the app ---------------------------------------------------------------------
    save_settings(admin, preferred_method="ldap")
    html = admin.get("/admin").text
    m = re.search(r'/admin/users/(\d+)/toggle-active"[^<]*<input[^>]*>\s*<button class="link">[^<]*</button>\s*</form>\s*</td>\s*</tr>', html)
    uid = None
    for row in re.findall(r"<tr>(.*?)</tr>", html, re.S):
        if ">jdoe" in row:
            uid = re.search(r"/admin/users/(\d+)/toggle-active", row).group(1)
    admin.post(f"/admin/users/{uid}/toggle-active", data={"csrf": csrf(html)})
    check(login(client(), "jdoe", PW).status_code == 403, "disabled in the app: AD login refused")
    admin.post(f"/admin/users/{uid}/toggle-active", data={"csrf": csrf(html)})

    idp.down()
    print("\nAll checks passed" if not failures else f"\n{failures} check(s) FAILED")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
