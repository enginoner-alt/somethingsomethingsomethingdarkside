# Milestones

Each milestone ends with: code pushed, README updated with run/test
instructions, and a **stop for review**. Next milestone starts only after the
owner tests and approves.

Testers: owner + Claude. Target hardware: laptop, RTX 4060 8 GB, 16 GB RAM.

## M0 — Skeleton
- Docker Compose: `caddy`, `api`, `postgres`, `worker` (stub), `llm-worker` (stub).
- Caddy with mounted wildcard cert; HTTP → HTTPS.
- Local accounts, break-glass admin created from env on first start.
- i18n plumbing with `locales/en.json`, `locales/tr.json`; language switcher.
- DB migrations (Alembic), job queue table + worker loop that picks up a no-op job.
- CI: lint + tests on GitHub Actions.

**Done when:** `docker compose up` → login page over HTTPS on your domain;
admin logs in; a test job goes queued → done.

## M1 — Safe login
Priority order (owner decision): **safe login first, then record and push.**

- Local login hardening: brute-force throttling per username and per IP,
  idle session timeout, sessions revoked when a user is disabled or their
  password changes.
- `LOCAL_LOGIN_MODE`: `all` (POC default) or `break_glass_only` (only the
  bootstrap admin may use a password once SSO is in place).
- Optional OIDC login (off unless configured): PKCE + nonce, users matched by
  issuer + subject (never by username), optional required group, IdP group →
  admin role, logout also ends the IdP session.
- Test harness (**sandbox/dev only, not part of the POC deployment**):
  OpenLDAP with dummy users and groups → Keycloak (LDAP federation, TOTP MFA) → app.

**Done when:** in the harness, an LDAP user logs in through Keycloak with TOTP;
the admin group grants admin; a user removed from the required group or deleted
from LDAP can't log in; local login is throttled after repeated failures; and the
break-glass admin still works when the IdP is down. Production AD = Keycloak
(or another IdP) configuration only, no app changes.

## M2 — Record and push

**Status:** built. Browser-tested with a fake microphone (`dev/recorder-test`,
also in CI): normal, 4 s network drop, flaky network, pause/resume, tab
closed, existing-file upload, Safari-style MP4 chunks, other user locked out.
Logged in as Samba AD users in the sandbox. Stereo kept (2 channels).
**Still open for the owner's devices:** the 60-minute run, real iOS/Android,
the real XVF3800 channel behaviour. Not built yet: per-recording channel
choice (D22, needed in M3).

- Mic picker (`enumerateDevices`), level meter, browser-DSP toggle (default off).
- Consent notice before recording.
- Chunked recording/upload with retry, pause/resume, Wake Lock.
- Finalize → reassembly → ffprobe validation; playback in browser.
- Upload existing audio files.
- Recording list, delete → trash (30 days) → hard delete; download audio.
- Per-user quotas enforced (storage, max length).

**Done when:** a 60-minute recording with the **ReSpeaker XVF3800** plays back
intact from: Linux, Windows, Android, iOS. Pulling the network for ~30 s
mid-recording loses no audio. XVF3800 channel behaviour documented.

## M3 — Transcription

**Status:** built and tested end to end against fake services (the browser
test runs in CI): 16 kHz mono FLAC prep (mix or one channel, 4-channel test
file), WhisperX client, auto-transcribe flag + button, transcript page,
speakers, admin Services page with Verify. **Not yet:** a run against the real
WhisperX on the 4060 (the "done when" below), transcript text editing, exports.

- Transcription via a **community WhisperX service** container (D25; first
  candidate `onerahmet/openai-whisper-asr-webservice`, GPU image, whisperx
  engine), pinned to a tested version. Our worker is a small client that sends
  audio and stores the result, behind a swappable connector. Models cached in
  a volume, HF token from env.
- First task: verify the service supports what we need (diarization with
  min/max speakers, word timestamps, Turkish alignment, long files) on the
  4060. If not, try the next candidate before writing our own.
- Transcribe → align → diarize full recording; store segments, words, speaker embeddings.
- Admin settings: model, compute type, batch size, default language, diarization, speaker hints.
- Transcript view: speaker-coloured segments, click-to-seek, rename speakers, edit text.
- Re-transcribe with language/model override.
- Exports: MD, TXT, SRT, VTT, JSON.

**Done when:** a real English meeting and a real Turkish meeting (≥30 min, ≥3
speakers) are transcribed and diarized on the 4060 without OOM; processing time
recorded in README.

## M4 — Insights

**Status:** built against the fake LLM (pulled forward together with M3):
summary, meeting subject, speakers (with "use these names"), action items,
decisions; editable prompts; long transcripts in parts. Also built:
- **Ask**: chats with follow-ups over one or more meetings, capped by the
  admin's "max context per Ask chat".
- **Transcript editing**: correct the text or speaker of any line; the page
  says how many words changed and marks insights made from the older text,
  with "run them again" (the user decides; no extra LLM call to judge the
  diff, the word count proved enough).
- **Banned languages** for automatic detection (e.g. never `az`, use `tr`).

**Not yet:** a real LLM, the GPU-sharing toggle, personal (per-user)
templates, LLM tags / meeting type.

- Admin: LLM endpoints (URL, model, key, context size, purpose), GPU-sharing toggle.
- Templates: global (admin) + personal (user); sensible defaults
  (summary, action items, decisions, open questions).
- On-demand insight run; map-reduce for transcripts longer than context.
- LLM generates title, tags, meeting type (user-editable).
- Transcript edit → user prompted → change-check (diff size, then small LLM call
  on the diff) → user decides whether to regenerate.

**Done when:** default templates produce useful output on the M3 meetings;
GPU-sharing toggle verified with LLM on the same 4060 (no OOM).

## Later (not scheduled)
- "Download all my data" zip; trash purge job verification.
- Cross-recording search (Q&A over meetings you pick is done: Ask).
- Voiceprint speaker suggestions (needs an embedding service, see D25).
- Multiple workers.
- DOCX export.
- Podcast engine: the LLM writes a script from a meeting's insights, and a TTS
  service (another container/URL, like the LLM; D25) voices it. Mind GPU
  memory on 8 GB.
- Splitting stacks across machines: test it, add TLS for the database
  connection, and the firewall rules from `docs/NETWORK.md`.
- Written Postgres major-version upgrade procedure; CI test of an upgrade path
  (recreate the db container on an existing volume, data intact).
- Rebuild-safety: all state in one host `data/` folder, read-only containers,
  logs to the host, more secrets as files (`secrets/`), optional edge stack /
  DMZ (and the DMZ in the diagrams).
- Admin-side manual linking of an SSO account and an AD account, for IdPs
  that can't send the objectGUID.
- E2E of SSO federated to a real AD (Keycloak → Samba AD) including the
  objectGUID claim linking.
