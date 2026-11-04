**somethingsomethingsomethingINcomplete**

![Something something something dark side](https://gifdb.com/images/high/something-dark-side-palpatine-animation-7jq8zzvws3y72ylr.webp)

# somethingsomethingsomethingdarkside

Self-hosted meeting recorder: record from any device and any mic in the
browser, transcribe and diarize with WhisperX on your own GPU, and get insights
from your own local LLM. Your server, your models, your data.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design and decisions log
- [docs/NETWORK.md](docs/NETWORK.md): stacks, networks, who may talk to whom, egress guard
- [docs/DIAGRAMS.md](docs/DIAGRAMS.md): architecture diagrams (system, deployment, recording flow, SSO, login safety, jobs)
- [docs/MILESTONES.md](docs/MILESTONES.md): milestones and acceptance criteria
- [docs/WINDOWS-WSL.md](docs/WINDOWS-WSL.md): running the POC on Windows via WSL2 (production uses the Linux instructions below)

**Status: M3 (transcription + insights) built and tested end to end with fake services.** Record or
upload, transcribe via a WhisperX service, ask an OpenAI-compatible LLM for insights.
Not yet run against a real WhisperX or LLM.

## Requirements

- Docker with the Compose plugin (`docker compose version`), and `bash`
  (the `./darkside` helper script).
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
   ./darkside up --build
   ```

   This starts two independent stacks: `db` (Postgres) and `app` (Caddy,
   API, workers). See [docs/NETWORK.md](docs/NETWORK.md) for who may talk to
   whom, then check it:

   ```bash
   ./darkside check
   ```

4. Open `https://<SITE_ADDRESS>` and log in as `admin` with the password you set.

## Recording (M2)

The home page lists your recordings (search by title, upload a file).
**New recording** (or **Record** in the top bar) opens the recorder:

1. Tick the consent box (text configurable with `CONSENT_NOTICE`).
2. Click **Allow microphone** and pick the mic. Leave the browser
   noise-suppression box **off** for conference mics like the XVF3800.
3. Press the red button to start, **Pause / Resume** as needed, and the red
   button (or **Stop**) to finish.

While recording, the browser sends a piece every `RECORDER_CHUNK_SECONDS`
(default 10). Each piece stays in the browser until the server confirms it,
so a network drop only delays the upload: the status line says "offline",
recording continues, and the queue catches up when the network returns.
Recordings are stored in the `darkside_audio` volume, never in the database
or the container.

- **Upload an existing file:** webm, ogg, opus, mp4, m4a, mp3, wav, flac, aac.
- **Tab closed or browser crashed:** after `RECORDING_STALE_SECONDS` (default
  10 min) with no new piece, the recording is kept as **interrupted** with
  everything that arrived.
- **Limits:** `MAX_RECORDING_SECONDS` (4 h), `USER_QUOTA_MB` (20 GB per user,
  0 = unlimited), `MAX_UPLOAD_BYTES` (2 GB).
- **Delete** moves a recording to the trash; it's removed for good after
  `TRASH_RETENTION_DAYS` (30).
- Only the owner can list, play, download or delete a recording. Not even
  admins can.

How it's tested (fake microphone, tone markers, network drops, pause, tab
close, Safari-style MP4): [dev/recorder-test/README.md](dev/recorder-test/README.md).

## Look and language

The pages use the Pantone palette 485 C (red, for record and primary
actions), Cool Gray 8 C and 877 C (text and lines) and 871 C (gold
accents), with Source Serif 4 for headings and Montserrat for everything
else. Tiempos isn't free to bundle, so Source Serif 4 stands in for it.
Both fonts are in `server/app/static/fonts/` (SIL Open Font License) and
are served by the app itself: no Google Fonts or other outside requests.

Every text on screen comes from `server/app/locales/<lang>.json`, including
server errors (sent as codes and translated in the browser) and the Verify
results. Dates follow the page's language.

## Transcription and insights (M3)

Configure in **Admin → Services**:

- **WhisperX service URL** (the community `whisper-asr-webservice` with
  `ASR_ENGINE=whisperx`; see [docs/ASR-API.md](docs/ASR-API.md)).
- **Transcribe automatically** on save (switch), default language
  (`auto`/`en`/`tr`), default channel (`mix` or a channel number), speakers
  on/off with min/max hints, and **banned languages** with a fallback (e.g.
  never `az`, use `tr` instead: Whisper often hears Turkish as Azerbaijani).
  Whisper handles one language per recording.
- **LLM base URL + model** (any OpenAI-compatible server). An API key, if
  needed, goes in `secrets/llm/llm_api_key`.
- **Verify WhisperX / Verify LLM** buttons. The **insight prompts** are under
  **Admin → Prompts** (editable, with reset to defaults).

Before sending, the worker turns every recording into **16 kHz mono FLAC**,
either mixing all channels or using the one you choose.

On a recording's page:

- the transcript, with click-to-jump and the current line highlighted;
- speaker renaming;
- **Transcribe again** with another language or channel;
- buttons for **Summary**, **Meeting subject** (also renames the recording),
  **Who is speaking?** (name suggestions, with "use these names"), **Action
  items** and **Decisions**;
- an **Ask** box (see below).

Answers come in the transcript's language and are saved, with re-run and
delete. Long transcripts are done in parts.

### Ask: questions with follow-ups, over one or more meetings

**Ask** in the menu, the Ask box on a recording, or ticking meetings on the
recordings list and pressing **Ask about N selected**. A chat keeps the
conversation: each follow-up is sent with the meetings and the earlier
turns, and answers come in the language of the question. Chats are private
to their owner like the recordings themselves.

**Admin → Services → LLM → Max context per Ask chat** caps one request
(meetings + earlier turns + question, in characters; roughly 3–4 per token).
While picking meetings a bar shows how much of it they use, and meetings
that don't fit can't be started together. When a conversation grows, the
oldest turns are left out first; the meetings always stay. The chat's
instructions are editable under **Admin → Prompts → Ask (chat)**.

**Trying it without a GPU or LLM:** `./darkside fakes up -d` starts a fake
WhisperX and a fake LLM. Set the URLs `http://whisperx:9000` and
`http://llm:8000/v1` with model `fake-model`.

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
   ./darkside up
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
   `"worker": "worker-asr-1"` / `"worker-llm-1"` in the result.
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

**Group names.** `OIDC_REQUIRED_GROUP` / `OIDC_ADMIN_GROUP` are always
compared **exactly** (ignoring case) with what the identity provider sends:

| You write | Matches only | Use when the provider sends |
|---|---|---|
| `darkside-admins` | a group sent as `darkside-admins` | group names. With AD, make it send the group's **sAMAccountName** (unique in the domain) |
| `CN=darkside-admins,OU=Groups,DC=corp,DC=local` | that exact DN | AD's `memberOf` as-is (full DNs) |
| `3f2a9c1e-…` (GUID) | that group ID | Entra ID group object IDs |

There's deliberately no "match the CN inside any DN" shortcut. In AD a CN is
only unique within its OU, so another OU could hold a different group with
the same CN. Set `OIDC_GROUPS_CLAIM` to the claim your provider uses
(`groups`, `memberOf`, `roles` for Entra App Roles, …). Every SSO login logs
the groups it received (`./darkside app logs api | grep "OIDC login"`), so
you can copy the exact value from there.

**Client secret.** `OIDC_TOKEN_AUTH_METHOD=auto` (default) reads the
provider's discovery document and uses HTTP Basic unless the provider only
accepts the secret in the request body (`client_secret_post`). You can force
either one.

**Provider notes** (not tested here, these are commercial products; the app
uses only standard OIDC):

- **Microsoft Entra ID:** issuer `https://login.microsoftonline.com/<tenant-id>/v2.0`.
  Easiest: create App Roles `darkside-users` / `darkside-admins` and set
  `OIDC_GROUPS_CLAIM=roles`. Alternatively, for groups synced from AD, Entra
  can send `sAMAccountName` in the groups claim. By default it sends GUIDs,
  and nothing at all above 200 groups.
- **PingFederate / PingOne, NetIQ Access Manager:** map the user's AD groups
  into a claim, preferably as sAMAccountNames, and set `OIDC_GROUPS_CLAIM` to
  its name. If they arrive as `memberOf` DNs, put the full DNs in the settings.
- **ManageEngine ADSelfService Plus:** use its OpenID Connect option for
  custom apps. The app doesn't speak SAML.

How this was tested (OpenLDAP + Keycloak + TOTP, 12 scenarios, also with
AD-style `memberOf` DNs and `client_secret_post`) and what to change for AD:
[dev/auth-test/README.md](dev/auth-test/README.md).

## Active Directory (direct login, the fallback next to SSO)

The app can check passwords against AD itself. It's set up in **Admin →
Login methods & Active Directory**:

1. Put the service account's password in `secrets/ldap_bind_password` (see
   [secrets/README.md](secrets/README.md)) and restart the api. A read-only
   account is enough.
2. If your DCs' certificates come from your internal CA (AD CS), put that CA's
   certificate in `deploy/ca/`.
3. In the admin page, fill in:
   - the DCs, e.g. `ldaps://dc01.corp.local:636`, one per line, tried in order;
   - the service account's DN;
   - the search base;
   - the required group and the admin group (sAMAccountName or full DN, matched
     exactly; nested groups count).
4. Click **Verify AD**, optionally with a test user and password. It shows each
   DC, the certificate, the service account, and what that user's login would
   see.

**Which method is used:**

- If SSO and AD both work, the **preferred** one is used (default: SSO). While
  SSO is preferred and working, AD passwords are refused and users are sent to
  the SSO button.
- If only one works, that one is used. If SSO is down, the login page says so
  and takes AD passwords.
- If neither works, only local accounts can log in (break-glass admin, test account).

Status is re-checked every 30 seconds. **Verify SSO** and **Verify AD** check
immediately.

**Security notes:**

- An AD password alone has no MFA. That's why SSO is the default preference.
- The password form only ever sends passwords to AD over LDAPS or StartTLS.
  Plain LDAP is refused, and empty passwords are never sent (AD would treat
  them as an anonymous login).
- Local accounts win over AD accounts with the same name, so AD can't take
  over `admin`.
- Login throttling applies to AD logins too. That also protects your users
  from AD lockouts triggered through this app.
- One person, one account: set `OIDC_AD_GUID_CLAIM` so SSO and AD logins of
  the same person link by AD `objectGUID`.

How it was tested (Samba AD DC, 26 checks): [dev/ad-test/README.md](dev/ad-test/README.md).

## Operations

**Upgrading from the single `docker-compose.yml` version:** stop the old
containers first. Run this *before* updating the code (it needs the old file),
or with `-p darkside` afterwards:

```bash
docker compose -p darkside down
```

Then `./darkside up --build`. Your data is kept: the database volume has the
same name (`darkside_pgdata`).


```bash
./darkside ps
```

```bash
./darkside logs api worker-asr worker-llm postgres
```

```bash
./darkside down
```

Any Docker Compose command works on a single stack too, e.g.
`./darkside db exec postgres psql -U darkside` or `./darkside app restart api`.

Data lives in the `darkside_pgdata` Docker volume. `./darkside down` keeps
it. `./darkside db down -v` **deletes it**.

**Forgot the admin password?** Set a new `BOOTSTRAP_ADMIN_PASSWORD`, set
`BOOTSTRAP_ADMIN_RESET=true`, then `./darkside app up -d api`. Set it back to
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
darkside                 helper: create networks, start/stop/check the stacks
deploy/app/compose.yml   app stack: caddy, api, worker-asr, worker-llm
deploy/db/               db stack: official Postgres + egress guard
deploy/asr/              transcription stack (WhisperX, from M3)
deploy/guard/            egress guard for passive services
deploy/check-isolation.sh  ./darkside check: verifies the network rules
deploy/Caddyfile         TLS + security headers (mic allowed, camera/geo off)
server/app/              FastAPI app, job queue, worker, templates, locales
server/alembic/          database migrations (applied on api start)
server/tests/            pytest suite (Postgres)
dev/auth-test/           SSO test harness: OpenLDAP + Keycloak + browser e2e (not deployed)
dev/ad-test/             AD test harness: Samba AD DC + e2e of the direct AD login (not deployed)
dev/recorder-test/       recorder e2e: fake mic + tone markers (runs in CI)
dev/fakes/               fake WhisperX + fake OpenAI-compatible LLM (tests / demo)
dev/m3-test/             M3 e2e: upload -> transcript -> insights in the browser (runs in CI)
dev/test-audio/          4-channel tone file
secrets/                 secret files, api only (git-ignored except README)
```
