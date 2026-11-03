# somethingsomethingsomethingdarkside

Self-hosted meeting recorder: record from any device and any mic in the
browser, transcribe and diarize with WhisperX on your own GPU, and get insights
from your own local LLM. Your server, your models, your data.

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): design and decisions log
- [docs/MILESTONES.md](docs/MILESTONES.md): milestones and acceptance criteria

**Status: M0 (skeleton) done.** You can log in, manage local users, and run test
jobs through the queue. Recording is M1.

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

2. Install the certificate: copy your wildcard **full chain** and **private key** to:

   ```
   deploy/certs/fullchain.pem
   deploy/certs/privkey.pem
   ```

   If your files have other names, change `CADDY_TLS` in `.env` (paths are
   inside `/certs`). This folder is git-ignored.

   *No certificate handy?* Set `SITE_ADDRESS=localhost` and `CADDY_TLS=internal`
   for a self-signed one. Your browser will warn about it, which is fine for a
   local test.

3. Start:

   ```bash
   docker compose up -d --build
   ```

4. Open `https://<SITE_ADDRESS>` and log in as `admin` with the password you set.

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
```
