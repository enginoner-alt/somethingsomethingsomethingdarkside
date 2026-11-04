#!/usr/bin/env bash
# Verify the network rules from inside the running containers (./darkside check).
# Each line tries a real TCP connection and compares it with what's allowed.
set -uo pipefail

fails=0
cid() { docker compose -p "darkside-$1" ps -q "$2" 2>/dev/null; }

# try <container-id> <host> <port>: exit 0 if a TCP connection succeeds (3 s).
try() {
  local c=$1 host=$2 port=$3
  if docker exec "$c" sh -c 'command -v python3' >/dev/null 2>&1; then
    docker exec "$c" python3 -c "
import socket, sys
try:
    socket.create_connection(('$host', $port), timeout=3).close()
except OSError:
    sys.exit(1)" >/dev/null 2>&1
  else
    docker exec "$c" sh -c "nc -z -w 3 $host $port" >/dev/null 2>&1
  fi
}

expect() { # expect allow|deny <label> <container-id> <host> <port>
  local want=$1 label=$2; shift 2
  if [[ -z $1 ]]; then echo "SKIP  $label (container not running)"; return; fi
  if try "$@"; then got=allow; else got=deny; fi
  if [[ $got == "$want" ]]; then
    echo "PASS  $label: $got"
  else
    echo "FAIL  $label: expected $want, got $got"
    fails=$((fails + 1))
  fi
}

api=$(cid app api)
caddy=$(cid app caddy)
wasr=$(cid app worker-asr)
wllm=$(cid app worker-llm)
pg=$(cid db postgres)

# An address outside Docker. Isolated containers must not reach it; the
# worker-llm control must, which proves the "deny" results are real and not
# just a machine without internet access.
OUTSIDE=${CHECK_OUTSIDE_HOST:-1.1.1.1}
OUTSIDE_PORT=${CHECK_OUTSIDE_PORT:-443}

echo "Allowed paths"
expect allow "caddy -> api:8000"            "$caddy" api 8000
expect allow "api -> postgres:5432"         "$api"   postgres 5432
expect allow "worker-asr -> postgres:5432"  "$wasr"  postgres 5432
expect allow "worker-llm -> postgres:5432"  "$wllm"  postgres 5432
expect allow "worker-llm -> outside (control)" "$wllm" "$OUTSIDE" "$OUTSIDE_PORT"
expect allow "api -> outside (IdP)"         "$api"   "$OUTSIDE" "$OUTSIDE_PORT"

echo "Blocked paths"
expect deny  "caddy -> postgres:5432"       "$caddy" postgres 5432
expect deny  "postgres -> api:8000"         "$pg"    api 8000
expect deny  "postgres -> caddy:443"        "$pg"    caddy 443
expect deny  "postgres -> outside"          "$pg"    "$OUTSIDE" "$OUTSIDE_PORT"
expect deny  "worker-asr -> outside"        "$wasr"  "$OUTSIDE" "$OUTSIDE_PORT"
expect deny  "worker-asr -> caddy:443"      "$wasr"  caddy 443
expect deny  "worker-llm -> caddy:443"      "$wllm"  caddy 443

echo "Published host ports (only caddy may have any)"
while read -r name ports; do
  [[ -z $name ]] && continue
  # Only "host:port->port" entries are published; "8000/tcp" is just exposed.
  published=$(tr ',' '\n' <<<"$ports" | grep -- '->' | xargs)
  if [[ -n $published && $name != *caddy* ]]; then
    echo "FAIL  $name publishes $published"; fails=$((fails + 1))
  elif [[ -n $published ]]; then
    echo "PASS  $name: $published"
  else
    echo "PASS  $name: none"
  fi
done < <(docker ps --filter "name=darkside-" --format '{{.Names}} {{.Ports}}')

echo
if ((fails)); then echo "$fails check(s) FAILED"; exit 1; fi
echo "All isolation checks passed"
