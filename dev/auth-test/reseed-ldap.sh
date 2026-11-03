#!/bin/sh
# Wipe the dummy people/groups and load seed.ldif again (test harness only).
set -eu
LDAP_URL=${LDAP_URL:-ldap://127.0.0.1}
BIND="-x -H $LDAP_URL -D cn=admin,dc=darkside,dc=test -w ${LDAP_BIND_PASSWORD:-ldap-admin-pw}"
for ou in people groups; do
  ldapdelete $BIND -r "ou=$ou,dc=darkside,dc=test" 2>/dev/null || true
done
ldapadd $BIND -f "$(dirname "$0")/seed.ldif" >/dev/null
echo "LDAP reseeded"
