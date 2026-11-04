#!/bin/sh
# Egress guard for passive services (postgres, later WhisperX).
#
# Installs firewall rules in this container's own network namespace:
#   - loopback: allowed
#   - packets belonging to connections someone else opened (replies): allowed
#   - any NEW outgoing connection: dropped
# so the service can answer its partners but never start a connection itself.
#
# Runs as root with CAP_NET_ADMIN only long enough to install the rules, then
# execs the image's normal entrypoint, which drops to the unprivileged service
# user (who can't change the rules). Fails closed: if the rules can't be
# installed, the container doesn't start. EGRESS_GUARD=off skips it (debug only).
set -eu

if [ "${EGRESS_GUARD:-on}" = "off" ]; then
  echo "egress-guard: DISABLED by EGRESS_GUARD=off" >&2
  exec "$@"
fi

apply() { # apply <iptables-binary>
  ipt=$1
  $ipt -F OUTPUT
  $ipt -A OUTPUT -o lo -j ACCEPT
  $ipt -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
  $ipt -A OUTPUT -j DROP
}

ok=""
for v4 in iptables iptables-legacy; do
  if command -v "$v4" >/dev/null 2>&1 && apply "$v4" 2>/dev/null; then ok=$v4; break; fi
done
if [ -z "$ok" ]; then
  echo "egress-guard: could not install firewall rules (needs cap_add: NET_ADMIN and iptables)." >&2
  echo "egress-guard: refusing to start unprotected. Set EGRESS_GUARD=off only to debug." >&2
  exit 1
fi
# IPv6: same rules if the namespace has IPv6 (Docker's default is IPv4 only).
v6=$(echo "$ok" | sed 's/iptables/ip6tables/')
if command -v "$v6" >/dev/null 2>&1; then apply "$v6" 2>/dev/null || true; fi

echo "egress-guard: outbound connections blocked ($ok); replies to incoming allowed" >&2
exec "$@"
