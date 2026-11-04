# Architecture diagrams

GitHub renders these Mermaid diagrams directly. Legend used throughout:

- **Solid green**: built and tested (M0, M1)
- **Dashed blue**: planned (M2+), shape agreed in [ARCHITECTURE.md](ARCHITECTURE.md)
- **Grey**: external, not part of this repo (you run or choose it)

## 1. System overview

Containers talk to each other over the network (decision D25). Only Caddy is
reachable from outside. Workers never talk to browsers: they claim jobs from
Postgres and write results back there.

```mermaid
flowchart TB
    subgraph clients["Clients: any device, any mic"]
        direction LR
        mic["Mic: ReSpeaker XVF3800 USB,<br/>built-in, 3.5 mm"]
        laptop["Laptop browser<br/>Windows / Linux / macOS"]
        phone["Phone browser<br/>Android / iOS"]
        mic -. "getUserMedia" .-> laptop
    end

    idp["Identity provider: optional<br/>Entra / Ping / NetIQ / ADSSP / Keycloak<br/>AD/LDAP + MFA live here"]

    subgraph host["docker compose: app host"]
        direction TB
        caddy["caddy<br/>TLS: deploy/certs<br/>HSTS, security headers"]
        ca["deploy/ca<br/>extra trusted CAs"]
        api["api: FastAPI<br/>web UI, login + SSO, admin panel,<br/>throttling, signed-cookie sessions"]
        upload["chunk upload + reassembly<br/>ffmpeg normalise"]
        pg[("postgres: official image<br/>users, login-failure counters,<br/>job queue, transcripts, insights")]
        audio[("volume: audio<br/>original recordings")]
        worker["worker: ours, small, no GPU<br/>claims jobs, calls services,<br/>stores results"]

        caddy -- ":8000" --> api
        ca -. "trusted by" .-> api
        api --> upload
        upload -- "write" --> audio
        api -- "SQL" --> pg
        worker -- "claim jobs SKIP LOCKED,<br/>save results" --> pg
        worker -- "read" --> audio
    end

    subgraph services["Services: same box or elsewhere, each just a URL"]
        direction LR
        asr["WhisperX service<br/>community GPU image<br/>transcribe + align + diarize"]
        models[("volume: models")]
        llm["LLM server<br/>OpenAI-compatible API"]
        tts["TTS service<br/>podcast engine, later"]
        asr --- models
    end

    laptop -- "HTTPS 443" --> caddy
    phone -- "HTTPS 443" --> caddy
    laptop -. "login redirect" .-> idp
    phone -. "login redirect" .-> idp
    api -- "OIDC over HTTPS" --> idp
    worker -- "HTTP: audio in,<br/>segments + speakers out" --> asr
    worker -- "HTTPS" --> llm
    worker -. "HTTPS, later" .-> tts

    classDef built fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    classDef planned fill:#e7f5ff,stroke:#1c7ed6,stroke-dasharray:5 4,color:#0b2e4f
    classDef external fill:#f1f3f5,stroke:#868e96,color:#212529
    class caddy,api,pg,ca built
    class upload,audio,worker planned
    class asr,models,llm,tts planned
    class idp,phone,laptop,mic external
    style clients fill:#fafafa,stroke:#adb5bd
    style host fill:#fafafa,stroke:#adb5bd
    style services fill:#fafafa,stroke:#adb5bd
```

The worker is partly built: the job queue, claiming, retries, heartbeats and
stale-job recovery exist and are tested. It's shown as planned because its
real work (calling the WhisperX and LLM services) arrives in M3/M4. Today it
only runs test jobs.

## 2. Deployment options

### POC: one laptop

```mermaid
flowchart TB
    subgraph laptop["Laptop: Windows + WSL2, RTX 4060 8 GB, 16 GB RAM"]
        direction LR
        subgraph compose["docker compose"]
            caddy --> api
            api --> pg[("postgres")]
            worker --> pg
        end
        asr["WhisperX service<br/>GPU"]
        worker --> asr
    end
    llm["LLM server<br/>private network"]
    worker --> llm
    users["Browsers on the LAN"] -- "HTTPS" --> caddy

    classDef gpu fill:#fff3bf,stroke:#f08c00,color:#5c3c00
    class asr gpu
```

### Production: split out as needed

Each arrow is just a URL in `.env` or the admin panel, so pieces move to other
machines without code changes.

```mermaid
flowchart TB
    users["Browsers"] -- "HTTPS" --> caddy

    subgraph app["App server: docker compose"]
        caddy --> api
        worker
    end

    subgraph db["Existing Postgres: optional"]
        pg[("company Postgres<br/>backups, monitoring, HA")]
    end

    subgraph gpu["GPU box(es)"]
        asr1["WhisperX service #1"]
        asr2["WhisperX service #2"]
    end

    storage[("shared audio storage<br/>volume / NFS / object store")]
    llm["LLM server"]
    idp["Identity provider<br/>AD + MFA"]

    api --> pg
    worker --> pg
    api --> storage
    worker --> storage
    worker --> asr1
    worker -.-> asr2
    worker --> llm
    api --> idp

    classDef gpu fill:#fff3bf,stroke:#f08c00,color:#5c3c00
    classDef todo fill:#e7f5ff,stroke:#1c7ed6,stroke-dasharray:5 4,color:#0b2e4f
    class asr1,asr2 gpu
    class pg,storage todo
```

Pointing `DATABASE_URL` at an external Postgres is planned but not yet wired
into `docker-compose.yml` (see D25). More GPUs = more WhisperX services and
workers; they all share the Postgres job queue.

## 3. Recording → transcript → insights

M2 covers the upload half and M3/M4 the processing half. Everything here is planned.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant B as Browser
    participant A as api
    participant V as audio volume
    participant P as postgres
    participant W as worker
    participant S as WhisperX service
    participant L as LLM server

    U->>B: pick mic, accept consent notice, press Record
    B->>A: POST /api/recordings
    A->>P: create recording (status: recording)
    A-->>B: recording id
    loop every ~10 s while recording
        B->>B: MediaRecorder chunk n (Wake Lock keeps screen on)
        B->>A: PUT /api/recordings/{id}/chunks/{n}
        A->>V: append chunk n (idempotent)
        A-->>B: ack n
        Note over B,A: Unacked chunks stay in memory and are retried,<br/>so a network blip loses nothing
    end
    U->>B: press Stop
    B->>A: POST /api/recordings/{id}/finalize {last: n}
    A->>V: check 0..n contiguous, concatenate, ffprobe
    A->>P: status: uploaded, enqueue job "transcribe" (gpu queue)

    W->>P: claim job (FOR UPDATE SKIP LOCKED)
    W->>V: read audio, ffmpeg → 16 kHz mono, pick channel (D22)
    W->>S: audio + language + speaker hints
    S-->>W: segments, word timestamps, SPEAKER_00…
    W->>P: store transcript, status: transcribed

    U->>B: rename speakers, fix text, ask for insights
    B->>A: POST /api/recordings/{id}/insights {template}
    A->>P: enqueue job "insights" (llm queue)
    W->>P: claim job
    W->>L: transcript (map-reduce if long) + template prompt
    L-->>W: summary, action items, title, tags…
    W->>P: store insights
    B->>A: poll / refresh
    A-->>B: transcript + insights
```

## 4. SSO login (OIDC)

Built and tested (M1) against Keycloak with LDAP and TOTP, over plain HTTP
and full HTTPS.

```mermaid
sequenceDiagram
    autonumber
    actor U as User
    participant B as Browser
    participant A as api
    participant I as Identity provider
    participant D as AD / LDAP

    U->>B: click "Sign in with SSO"
    B->>A: GET /auth/oidc/login
    A->>I: GET /.well-known/openid-configuration (once, cached)
    A-->>B: 302 to IdP with state, nonce, PKCE S256 challenge
    B->>I: login page
    U->>B: types username + password
    B->>I: POST credentials
    I->>D: verify password, read groups
    I-->>B: MFA prompt
    U->>B: TOTP code / passkey
    B->>I: POST second factor (MFA policy lives in the IdP)
    I-->>B: 302 to /auth/oidc/callback?code=…&state=…
    B->>A: GET /auth/oidc/callback
    A->>I: POST token endpoint: code + PKCE verifier<br/>secret via Basic or form body (OIDC_TOKEN_AUTH_METHOD)
    I-->>A: ID token (+ userinfo if groups missing)
    A->>A: verify ID token + nonce, read groups claim<br/>exact match: required group, admin group
    alt not in required group / disabled in app
        A-->>B: login page with reason (403)
    else allowed
        A->>A: find user by (issuer, subject), never by username<br/>update name + admin flag, new session
        A-->>B: 303 to the app
    end
    Note over A,I: IdP slow or unreachable: "sign-in service unreachable"<br/>page, and the break-glass local admin still works
```

## 5. Local login safety

```mermaid
flowchart TD
    start(["POST /login"]) --> csrf{"CSRF token ok?"}
    csrf -- no --> e400["400"]
    csrf -- yes --> thr{"throttled?<br/>5 fails per user+IP or<br/>20 per IP in 15 min"}
    thr -- yes --> e429["429: too many attempts<br/>password not even checked"]
    thr -- no --> mode{"LOCAL_LOGIN_MODE =<br/>break_glass_only and<br/>not the admin?"}
    mode -- yes --> fail
    mode -- no --> pw{"password correct and<br/>account enabled?"}
    pw -- no --> fail["record failure<br/>401: wrong username or password"]
    pw -- yes --> ok["clear failures for user+IP<br/>new session: user, auth_version, last seen"]
    ok --> app(["303 to the app"])

    req(["any later request"]) --> chk{"session valid?<br/>user active, auth_version same,<br/>idle under 2 h, age under 12 h"}
    chk -- yes --> serve["serve page, refresh last seen"]
    chk -- no --> relog["clear session, back to login"]

    classDef bad fill:#ffe3e3,stroke:#c92a2a,color:#5c0a0a
    classDef good fill:#d3f9d8,stroke:#2b8a3e,color:#0b3d17
    class e400,e429,fail,relog bad
    class ok,app,serve good
```

Disabling a user in the admin panel bumps their `auth_version`, which ends
every open session at the next request.

## 6. Job lifecycle

Built (M0) and used by every background task.

```mermaid
stateDiagram-v2
    [*] --> queued: enqueue
    queued --> running: worker claims (SKIP LOCKED),<br/>attempts + 1
    running --> done: handler returns result
    running --> queued: handler fails,<br/>attempts left
    running --> failed: handler fails,<br/>no attempts left
    running --> queued: heartbeat older than 5 min<br/>(worker died), attempts left
    running --> failed: heartbeat stale,<br/>no attempts left
    done --> [*]
    failed --> [*]

    note right of running
        A background thread refreshes the heartbeat
        every 30 s while the handler runs
    end note
```
