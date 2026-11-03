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

## M1 — Capture
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

## M2 — Transcription
- WhisperX worker image (CUDA), models cached in a volume, HF token from env.
- Transcribe → align → diarize full recording; store segments, words, speaker embeddings.
- Admin settings: model, compute type, batch size, default language, diarization, speaker hints.
- Transcript view: speaker-coloured segments, click-to-seek, rename speakers, edit text.
- Re-transcribe with language/model override.
- Exports: MD, TXT, SRT, VTT, JSON.

**Done when:** a real English meeting and a real Turkish meeting (≥30 min, ≥3
speakers) are transcribed and diarized on the 4060 without OOM; processing time
recorded in README.

## M3 — Insights
- Admin: LLM endpoints (URL, model, key, context size, purpose), GPU-sharing toggle.
- Templates: global (admin) + personal (user); sensible defaults
  (summary, action items, decisions, open questions).
- On-demand insight run; map-reduce for transcripts longer than context.
- LLM generates title, tags, meeting type (user-editable).
- Transcript edit → user prompted → change-check (diff size, then small LLM call
  on the diff) → user decides whether to regenerate.

**Done when:** default templates produce useful output on the M2 meetings;
GPU-sharing toggle verified with LLM on the same 4060 (no OOM).

## M4 — Auth & admin hardening
- OIDC login (tested against Keycloak or Authentik federated to AD, with MFA).
- IdP group → role mapping (e.g. `meeting-admins` → admin).
- Local login can be disabled except break-glass admin.
- "Download all my data" zip; trash purge job verified.

**Done when:** AD user logs in through the IdP with MFA; group-based admin works;
user deletes everything and nothing remains on disk/DB after purge.

## M5 — Later (not scheduled)
Cross-recording search/Q&A, voiceprint speaker suggestions, multiple workers,
DOCX export.
