# Network layout and isolation

Rules (decision D26):

1. **Only the side that starts connections knows URLs.** The app stack (api +
   workers) holds `DATABASE_URL` and, later, the WhisperX, LLM and TTS URLs.
   Passive services (Postgres, WhisperX) get no URLs, only their own settings.
2. **Each service can only talk to its partners.** Docker networks decide who
   can reach whom; internal networks have no route to the internet.
3. **Passive services never start a connection at all.** An egress guard
   (firewall inside the container) drops every connection they try to open.
   Replies to their partners still work.

## Stacks

Each stack is its own Docker Compose project with its own lifecycle:

| Stack | File | Services | Starts connections to |
|---|---|---|---|
| `db` | `deploy/db/compose.yml` | postgres (official image + egress guard) | nobody |
| `app` | `deploy/app/compose.yml` | caddy, api, worker-asr, worker-llm | postgres, WhisperX, LLM, TTS, IdP |
| `asr` (M3) | `deploy/asr/compose.yml` | WhisperX service (community image + egress guard) | nobody |

On one machine, `./darkside up` creates the networks and starts `db`, then
`app`. Each stack can also be handled on its own, e.g.
`./darkside db restart` or `./darkside app logs api`.

## Networks (one machine)

| Network | Internal (no internet) | Members | Purpose |
|---|---|---|---|
| `darkside-edge` | no | caddy, api | caddy publishes 80/443, proxies to api |
| `darkside-db` | **yes** | api, worker-asr, worker-llm, postgres | database traffic |
| `darkside-asr` | **yes** | worker-asr, WhisperX (M3) | transcription requests |
| `darkside-out` | no | api, worker-llm | outbound: IdP and AD domain controllers on 636/389 (api), LLM/TTS (worker) |

Who can reach whom, as verified by `./darkside check`:

| From \ To | caddy | api | postgres | internet |
|---|---|---|---|---|
| browser | ✅ 443 | ❌ | ❌ | |
| caddy | | ✅ | ❌ | (bridge) |
| api | ❌ | | ✅ | ✅ IdP, AD DCs |
| worker-asr | ❌ | ❌ | ✅ | ❌ |
| worker-llm | ❌ | ❌ | ✅ | ✅ LLM |
| **postgres** | ❌ | ❌ (guard) | | ❌ |

Only caddy publishes host ports. Postgres, api and workers publish none.

## Egress guard

`deploy/guard/egress-guard.sh` is the entrypoint wrapper of every passive
service. At container start it installs, in that container's own network
namespace:

```
-A OUTPUT -o lo -j ACCEPT                                    # loopback
-A OUTPUT -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT   # replies
-A OUTPUT -j DROP                                            # anything it starts
```

Then it hands over to the image's normal entrypoint, which drops to the
unprivileged service user. That user has no capabilities, so the service can't
change the rules. If the rules can't be installed (no `NET_ADMIN`, no
`iptables`), the container **refuses to start**. `EGRESS_GUARD=off` disables it,
for debugging only.

The guarded Postgres image is the official `postgres:16-alpine` plus
`iptables`, nothing else (`deploy/db/Dockerfile`).

## Checking it

```bash
./darkside check
```

It opens real TCP connections from inside each container and compares them
with the table above. It includes a control: worker-llm must reach an outside
address, so a "blocked" result can't come from a machine that simply has no
internet. By default the outside address is `1.1.1.1:443`; override it with
`CHECK_OUTSIDE_HOST` / `CHECK_OUTSIDE_PORT` if your network blocks that.

## Splitting stacks across machines

**Not tested yet.** This is the plan for when a stack moves to its own machine.

- **Postgres elsewhere:** on the DB machine, run only the `db` stack and
  publish 5432 **bound to the private interface**. On the app machine, set
  `DATABASE_URL=postgresql+psycopg://darkside:<password>@<db-host>:5432/darkside`
  in `.env` and run only the `app` stack (`./darkside app up -d`).
  Your company Postgres works the same way.
- **Firewall on the DB machine:** allow 5432 only from the app machine.
  Watch out: **ports published by Docker bypass `ufw`/`firewalld` rules.**
  Filter in Docker's `DOCKER-USER` chain instead:

  ```bash
  sudo iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport 5432 ! -s <app-host-ip> -j DROP
  ```

- **Encryption:** between machines the database connection should use TLS.
  That isn't configured yet and is needed before any cross-machine setup.
- **WhisperX elsewhere (M3):** same pattern. Run the `asr` stack on the GPU
  box, allow its port only from the app machine, and set the URL in the app's
  settings.

The egress guard keeps working on any machine, since it lives inside the
container, not on the host.
