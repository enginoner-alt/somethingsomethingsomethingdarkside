# Auth test harness (OpenLDAP → Keycloak → app)

**Test harness only. Not part of the POC or production deployment.**

This proves the app's SSO side end to end with a dummy directory:

```
OpenLDAP (seed.ldif)  →  Keycloak (LDAP federation, TOTP required)  →  OIDC  →  app
```

Production with Active Directory has the same shape. Only Keycloak's LDAP
provider settings change (see [Pointing it at AD](#pointing-it-at-ad)). The app
needs no changes.

## What it checks

`e2e.cjs` (Playwright) runs these checks:

| # | Scenario | Expected |
|---|----------|----------|
| 1 | `jdoe` first SSO login | forced TOTP enrolment, then lands in the app; name from LDAP |
| 2 | `jdoe` is in `darkside-admins` | admin panel allowed |
| 3 | log out | Keycloak session ends too; password asked again |
| 4 | `jdoe` second login | password **and** TOTP code required |
| 5 | `asmith` (users group only) | logs in, `/admin` is 403 |
| 6 | `mallory` (valid LDAP, not in `darkside-users`) | refused by the app |
| 7 | `asmith` removed from the group in LDAP | refused at next login |
| 8 | `mallory` deleted from LDAP | Keycloak rejects the login |
| 9 | SSO username on the local form | no local password, rejected |
| 10 | break-glass `admin` on the local form | works |

`e2e-idp-down.cjs` runs with Keycloak **stopped**: the SSO button fails
gracefully, and the break-glass admin can still log in.

Last runs in the Claude sandbox: all checks pass (Keycloak 26.0.7, OpenLDAP
slapd 2.6, Chromium), both ways:

- **plain HTTP** (app on `localhost:8000`, Keycloak on `localhost:8080`), and
- **full HTTPS, like production**: app behind Caddy at `https://rec.darkside.test`
  and Keycloak at `https://kc.darkside.test:8443`, both using a test wildcard
  chain from `../make-test-certs.sh`. The app trusts the test CA via `deploy/ca/`,
  with none of the insecure switches below. Three consecutive passes.

### AD-style variant (memberOf DNs + client_secret_post)

Mimics an AD-backed IdP that sends full group DNs:

1. `ldapmodify -Y EXTERNAL -H ldapi:/// -f enable-memberof.ldif` (once), then `./reseed-ldap.sh`.
2. `AD_STYLE_GROUPS=1 ./configure-keycloak.sh`. This adds a `memberOf` claim
   with values like `cn=darkside-admins,ou=groups,dc=darkside,dc=test`.
3. Run the app with `OIDC_GROUPS_CLAIM=memberOf`, the full DNs
   `OIDC_REQUIRED_GROUP=cn=darkside-users,ou=groups,dc=darkside,dc=test` /
   `OIDC_ADMIN_GROUP=cn=darkside-admins,ou=groups,dc=darkside,dc=test`, and
   `OIDC_TOKEN_AUTH_METHOD=client_secret_post`. Plain names deliberately do
   **not** match DNs.
4. `node e2e.cjs`

Last run: 12/12, with the DNs written in different case than the provider
sends them. The app log showed only DNs in `memberOf`, and a packet
capture of the token requests showed the secret in the form body and no
`Authorization: Basic` header. With `OIDC_TOKEN_AUTH_METHOD=auto` (Keycloak
supports both), the capture showed Basic and no secret in the body.

### HTTPS variant

1. `OUT_DIR=./test-certs ../make-test-certs.sh`, then copy `fullchain.pem` and
   `privkey.pem` to `deploy/certs/` and `ca.pem` to `deploy/ca/`.
2. Make `rec.darkside.test` and `kc.darkside.test` resolve to the machine, and
   trust `ca.pem` in the browser that runs the test.
3. Start Keycloak with `--https-port 8443 --https-certificate-file …/fullchain.pem
   --https-certificate-key-file …/privkey.pem --hostname https://kc.darkside.test:8443`.
4. Run `configure-keycloak.sh` with `KC_URL=https://kc.darkside.test:8443` and
   `APP_URL=https://rec.darkside.test`.
5. Start the stack with `SITE_ADDRESS=rec.darkside.test`, the default
   `CADDY_TLS`, and `OIDC_ISSUER=https://kc.darkside.test:8443/realms/darkside`.
   The api container must be able to resolve `kc.darkside.test`.
6. `APP_URL=https://rec.darkside.test node e2e.cjs`

## Dummy accounts (seed.ldif)

All LDAP passwords: `Passw0rd!`

| user | groups |
|------|--------|
| `jdoe` (Jane Doe) | `darkside-users`, `darkside-admins` |
| `asmith` (Alan Smith) | `darkside-users` |
| `mallory` | none |

## Running it

You need OpenLDAP (`slapd`, `ldap-utils`), Keycloak 26 (container or the
release tarball + Java 21), Node + Playwright, and the app.

1. **OpenLDAP** with base DN `dc=darkside,dc=test`, admin
   `cn=admin,dc=darkside,dc=test` / `ldap-admin-pw`. Then:

   ```bash
   ./reseed-ldap.sh
   ```

2. **Keycloak** in dev mode on port 8080. With Docker (this exact command is untested: the sandbox couldn't reach quay.io, so the recorded run used the release tarball):

   ```bash
   docker run -d --name kc-test --network host -e KC_BOOTSTRAP_ADMIN_USERNAME=kcadmin -e KC_BOOTSTRAP_ADMIN_PASSWORD=kcadmin-pw quay.io/keycloak/keycloak:26.0.7 start-dev --http-port 8080 --hostname http://localhost:8080
   ```

   Then configure it. **This deletes and recreates the `darkside` realm:**

   ```bash
   KCADM="docker exec kc-test /opt/keycloak/bin/kcadm.sh" ./configure-keycloak.sh
   ```

   (With the tarball, use `KCADM=/path/to/keycloak/bin/kcadm.sh`.)

3. **App** on `http://localhost:8000` with these settings (in addition to
   the database settings and `SECRET_KEY`):

   ```
   SESSION_COOKIE_SECURE=false          # plain HTTP in this harness only
   AUTHLIB_INSECURE_TRANSPORT=1         # lets OIDC talk to http://localhost:8080
   BOOTSTRAP_ADMIN_PASSWORD=admin-password-123
   OIDC_ISSUER=http://localhost:8080/realms/darkside
   OIDC_CLIENT_ID=darkside
   OIDC_CLIENT_SECRET=darkside-test-secret
   OIDC_REQUIRED_GROUP=darkside-users
   OIDC_ADMIN_GROUP=darkside-admins
   OIDC_BUTTON_LABEL=Keycloak
   ```

   Never use the first two outside this harness.

4. **Run** (from a clean app database, since TOTP enrolment happens only once per user):

   ```bash
   node e2e.cjs
   ```

   Stop Keycloak, then:

   ```bash
   node e2e-idp-down.cjs
   ```

To rerun, repeat `reseed-ldap.sh` and `configure-keycloak.sh`, and empty the
app's `users` table.

## Things the harness taught us (apply to production too)

- **Keycloak caches LDAP group membership.** With the default cache, a user
  removed from `darkside-users` could still log in. The harness sets the LDAP
  provider's **cache policy to `NO_CACHE`**, which makes group changes apply at
  the next login. Do the same with AD. At your user count the extra LDAP
  queries don't matter.
- **Group changes apply at the next login, not instantly.** The app reads
  groups when someone logs in. An existing app session lasts until its idle
  timeout (2 h by default) or absolute limit (12 h). To cut someone off
  immediately, also click **Disable** in the app's admin panel: that ends their
  sessions at once.
- **The default "first name" mapper reads `cn`** for generic LDAP, which
  doubled surnames ("Alan Smith Smith"). The script maps it to `givenName`.
  The AD vendor preset already does this correctly.
- **Making OTP mandatory:** set the "Browser - Conditional OTP" sub-flow to
  REQUIRED and disable its "Condition - user configured" step. Send the full
  execution object when updating it, or Keycloak resets its priority and asks
  for OTP *before* the password.

## Pointing it at AD

In Keycloak's LDAP provider, the differences from this harness are:

| setting | OpenLDAP (harness) | Active Directory |
|---|---|---|
| vendor | `other` | `ad` |
| connection URL | `ldap://…:389` | `ldaps://dc.example.local:636` |
| username attribute | `uid` | `sAMAccountName` |
| RDN attribute | `uid` | `cn` |
| UUID attribute | `entryUUID` | `objectGUID` |
| user object classes | `inetOrgPerson` | `person, organizationalPerson, user` |
| users DN | `ou=people,…` | your users OU |
| bind DN | LDAP admin | a read-only service account |
| group mapper object class | `groupOfNames` | `group` |
| group mapper name attribute | `cn` | **`sAMAccountName`** (unique in the domain; the CN is only unique per OU) |
| cache policy | `NO_CACHE` | `NO_CACHE` |

In AD, a disabled account (`userAccountControl`) is refused by Keycloak automatically.
