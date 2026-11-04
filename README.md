**somethingsomethingsomethingINcomplete**

![Something something something dark side](https://gifdb.com/images/high/something-dark-side-palpatine-animation-7jq8zzvws3y72ylr.webp)

# somethingsomethingsomethingdarkside

Self-hosted meeting recorder: record from any device and any mic in the
browser, transcribe and diarize with WhisperX on your own GPU, and get insights
from your own local LLM. Your server, your models, your data.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design and decisions log
- [docs/MILESTONES.md](docs/MILESTONES.md): milestones and acceptance criteria
- [docs/WINDOWS-WSL.md](docs/WINDOWS-WSL.md): running the POC on Windows via WSL2 (production uses the Linux instructions below)

**Status: M1 (safe login) done.** Local login is hardened, and optional SSO
via OIDC (Keycloak, AD, MFA) works and is tested end to end. Next: M2, record and push.

## Requirements

- Docker with the Compose plugin (`docker compose version`).
- A hostname for the app that your wildcard certificate covers (e.g.
  `rec.example.com`), with DNS (or a hosts-file entry) pointing it at this machine.
- Ports 80 and 443 free on this machine.
- From M2 on: an NVIDIA GPU with the NVIDIA Container Toolkit, and a Hugging Face token.

## Run it

1. Configure:

   ```bash
   cp .env.example .env
   ```

   Edit `.env`: set `SITE_ADDRESS`, then generate the secrets:

   ```bash
   sed -i "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$(openssl rand -hex 24)/; s/^SECRET_KEY=.*/SECRET_KEY=$(openssl rand -hex 32)/" .env
   ```

   Set `BOOTSTRAP_ADMIN_PASSWORD` to something you'll remember.

   Optional: set `TEST_ACCOUNT_PASSWORD` (at least 10 characters) to get a
   ready-made **local test account** (`test`, a regular user). See
   [Test account](#test-account).

2. Install the certificate: copy your wildcard **full chain** and **private key** to:

   ```
   deploy/certs/fullchain.pem
   deploy/certs/privkey.pem
   ```

   See [Certificates](#certificates) below. With a normal (public) wildcard
   certificate, that's all.

3. Start:

   ```bash
   docker compose up -d --build
   ```

4. Open `https://<SITE_ADDRESS>` and log in as `admin` with the password you set.

## Certificates

| Your certificate | What to do | Devices |
|---|---|---|
| **Public wildcard** (Let's Encrypt, DigiCert, Sectigo, …) **(recommended)** | Put `fullchain.pem` + `privkey.pem` in `deploy/certs/`. Done. | Every browser and phone trusts it automatically. |
| **Internal / self-signed CA** | Same two files in `deploy/certs/`, **plus** the CA certificate in `deploy/ca/` (e.g. `deploy/ca/company-ca.pem`). | Every device must trust that CA too (see below). |
| **None, just trying it out** | `SITE_ADDRESS=localhost` and `CADDY_TLS=internal` in `.env`. | Browser warning on the laptop. Phones won't work. |

Details:

- **`fullchain.pem` must contain the full chain:** your certificate first,
  then the intermediate certificate(s). Your provider usually ships it as
  "fullchain" or "bundle". If you only have `cert.pem` and `chain.pem`:
  `cat cert.pem chain.pem > fullchain.pem`. Without the intermediate, desktop
  browsers often still work but phones don't.
- **A wildcard covers one level:** `*.example.com` covers `rec.example.com`,
  but not `example.com` or `a.rec.example.com`.
- **Files named differently?** Set `CADDY_TLS` in `.env` (paths inside `/certs`).
- **`deploy/ca/`** is for servers *the app itself calls*, such as your
  Keycloak, when they use an internal CA. Drop the CA's `.pem`/`.crt` there and
  restart. The api log then shows `trusting extra CA certificate …`. Public
  CAs need nothing there.
- **Self-signed on phones is the hassle part.** On iOS you install the CA
  profile, then enable it under *Settings → General → About → Certificate
  Trust Settings*. On Android it's *Settings → Security → Encryption &
  credentials → Install a certificate → CA certificate*. With a public
  wildcard you skip all of this.

**Tested** (sandbox, all over HTTPS, no insecure switches): a test chain
(root → intermediate → `*.darkside.test`, made by `dev/make-test-certs.sh`)
served by Caddy from `deploy/certs/`; Keycloak on HTTPS with the same
wildcard; the app trusting the CA from `deploy/ca/`; the full SSO test 3× in a
row; and without the CA file, SSO refuses the certificate and shows a clear
message instead of crashing.

## Test account

For trying the app as a normal (non-admin) user on your home machine.

1. In `.env`:

   ```
   TEST_ACCOUNT_USERNAME=test
   TEST_ACCOUNT_PASSWORD=pick-a-password-123
   ```

2. Apply it:

   ```bash
   docker compose up -d
   ```

3. Log in as `test` with that password. Use a private window if you're
   logged in as admin in the normal one.

**Disable / enable:** *Admin → Users*. The test account has a "test account"
label; click **Disable**. Its open sessions end immediately and it can't log
in. Click **Enable** to allow it again. Restarting the app never re-enables a
disabled test account. Change `TEST_ACCOUNT_PASSWORD` and restart to give it a
new password.

**Remove it:** empty `TEST_ACCOUNT_PASSWORD` stops it being created on new
installs. An existing test account stays until you disable it.

The test account is a local password account. With
`LOCAL_LOGIN_MODE=break_glass_only` it can't log in (only the admin can).

## M0 acceptance test

1. `http://<SITE_ADDRESS>` redirects to `https://`.
2. Log in as admin. You land on the Recordings placeholder page.
3. Open **Admin → Jobs** and click **Run test job on "gpu"**, then **"llm"**.
   Within a few seconds both should show **done**, with
   `"worker": "worker-gpu-1"` / `"worker-llm-1"` in the result.
4. Create a second (non-admin) user, log in as that user in a private window,
   and confirm `/admin` returns 403.
5. Switch the language to Türkçe. The UI is translated and the choice persists
   after logging out and back in.

## M1: login safety

On by default, no configuration needed:

- **Brute-force throttling.** 5 failed logins per username from one IP, or 20
  from one IP in total, within 15 minutes. After that, logins from there are
  refused for the rest of the window, even with the right password. Another IP
  is unaffected, so nobody can lock the real admin out from elsewhere.
- **Idle timeout.** 2 hours without activity logs you out (12 hours absolute).
- **Instant revocation.** **Disable** in the admin panel ends that user's
  sessions immediately.

### SSO (optional, off in the POC)

Fill in the `OIDC_*` settings in `.env` (see `.env.example`) to add a
**Sign in with …** button. Then:

- AD/LDAP, MFA (TOTP, passkeys), password policies and lockouts are
  configured in the identity provider, not here.
- `OIDC_REQUIRED_GROUP` restricts who may log in. `OIDC_ADMIN_GROUP` grants
  admin, re-checked at every login.
- SSO users are matched by IdP identity, never by username. An IdP user
  called `admin` can't take over the local admin.
- `LOCAL_LOGIN_MODE=break_glass_only` hides the password form behind an
  "Emergency administrator login" link and allows only the bootstrap admin.
  That way you can still get in when the identity provider is down.

How this was tested (OpenLDAP + Keycloak + TOTP, 12 scenarios) and what to
change for AD: [dev/auth-test/README.md](dev/auth-test/README.md).

## Operations

```bash
docker compose ps
```

```bash
docker compose logs -f api worker-gpu worker-llm
```

```bash
docker compose down
```

Data lives in the `pgdata` Docker volume. `docker compose down` keeps it;
`docker compose down -v` **deletes it**.

**Forgot the admin password?** Set a new `BOOTSTRAP_ADMIN_PASSWORD`, set
`BOOTSTRAP_ADMIN_RESET=true`, then `docker compose up -d api`. Set it back to
`false` afterwards.

## Development

Tests need a Postgres database (the queue relies on `SKIP LOCKED`):

```bash
cd server && pip install -e ".[dev]"
```

```bash
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost/testdb pytest
```

```bash
ruff check . && ruff format --check .
```

The test database is **wiped** at the start of every run, so don't point it at real data.

### Adding a UI language

Copy `server/app/locales/en.json` to `xx.json`, translate the values, and set
`_meta.name` to the language's own name. A test fails if any locale is missing keys.

## Layout

```
docker-compose.yml       caddy, api, postgres, worker-gpu, worker-llm
deploy/Caddyfile         TLS + security headers (mic allowed, camera/geo off)
server/app/              FastAPI app, job queue, worker, templates, locales
server/alembic/          database migrations (applied on api start)
server/tests/            pytest suite (Postgres)
dev/auth-test/           SSO test harness: OpenLDAP + Keycloak + browser e2e (not deployed)
```
