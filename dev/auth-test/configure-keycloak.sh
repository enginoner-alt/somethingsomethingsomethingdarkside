#!/bin/sh
# Configure a throwaway Keycloak for the auth test harness:
#   realm "darkside" → LDAP user federation (OpenLDAP, seed.ldif) + group mapper
#   → TOTP required for everyone → OIDC client "darkside" with a groups claim.
#
# TEST HARNESS ONLY. Not part of the POC or production deployment.
# In production, the same shape applies with your AD: point the LDAP provider at
# AD (vendor "ad", usernameLDAPAttribute sAMAccountName, LDAPS) instead.
set -eu

KC_URL=${KC_URL:-http://localhost:8080}
KC_ADMIN=${KC_ADMIN:-kcadmin}
KC_ADMIN_PASSWORD=${KC_ADMIN_PASSWORD:-kcadmin-pw}
LDAP_URL=${LDAP_URL:-ldap://127.0.0.1:389}
LDAP_BIND_DN=${LDAP_BIND_DN:-cn=admin,dc=darkside,dc=test}
LDAP_BIND_PASSWORD=${LDAP_BIND_PASSWORD:-ldap-admin-pw}
APP_URL=${APP_URL:-http://localhost:8000}
CLIENT_SECRET=${CLIENT_SECRET:-darkside-test-secret}
KCADM=${KCADM:-kcadm.sh}

$KCADM config credentials --server "$KC_URL" --realm master --user "$KC_ADMIN" --password "$KC_ADMIN_PASSWORD"

# Start from scratch every time (throwaway test Keycloak only!).
$KCADM delete realms/darkside 2>/dev/null || true

$KCADM create realms -s realm=darkside -s enabled=true -s bruteForceProtected=true

# --- LDAP federation -------------------------------------------------------------
LDAP_ID=$($KCADM create components -r darkside -i \
  -s name=openldap -s providerId=ldap -s providerType=org.keycloak.storage.UserStorageProvider \
  -s 'config.vendor=["other"]' \
  -s 'config.editMode=["READ_ONLY"]' \
  -s "config.connectionUrl=[\"$LDAP_URL\"]" \
  -s "config.bindDn=[\"$LDAP_BIND_DN\"]" \
  -s "config.bindCredential=[\"$LDAP_BIND_PASSWORD\"]" \
  -s 'config.usersDn=["ou=people,dc=darkside,dc=test"]' \
  -s 'config.usernameLDAPAttribute=["uid"]' \
  -s 'config.rdnLDAPAttribute=["uid"]' \
  -s 'config.uuidLDAPAttribute=["entryUUID"]' \
  -s 'config.userObjectClasses=["inetOrgPerson"]' \
  -s 'config.searchScope=["1"]' \
  -s 'config.authType=["simple"]' \
  -s 'config.importEnabled=["true"]' \
  -s 'config.syncRegistrations=["false"]' \
  -s 'config.trustEmail=["true"]' \
  -s 'config.cachePolicy=["NO_CACHE"]')
# NO_CACHE: group changes in LDAP/AD apply at the user's next login. With the
# default cache, Keycloak keeps serving the old groups until the cache entry is
# evicted (found by e2e.cjs: a user removed from the group could still log in).

# Keycloak's default "first name" mapper for vendor "other" reads cn (the full
# name), so the name claim came out as "Alan Smith Smith". Use givenName.
FIRST_NAME_MAPPER=$($KCADM get components -r darkside -q parent="$LDAP_ID" -q name="first name" --fields id \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)[0]["id"])')
$KCADM update "components/$FIRST_NAME_MAPPER" -r darkside -s 'config."ldap.attribute"=["givenName"]'

$KCADM create components -r darkside \
  -s name=groups -s providerId=group-ldap-mapper \
  -s providerType=org.keycloak.storage.ldap.mappers.LDAPStorageMapper \
  -s parentId="$LDAP_ID" \
  -s 'config."groups.dn"=["ou=groups,dc=darkside,dc=test"]' \
  -s 'config."group.name.ldap.attribute"=["cn"]' \
  -s 'config."group.object.classes"=["groupOfNames"]' \
  -s 'config."membership.ldap.attribute"=["member"]' \
  -s 'config."membership.attribute.type"=["DN"]' \
  -s 'config.mode=["READ_ONLY"]' \
  -s 'config."user.roles.retrieve.strategy"=["LOAD_GROUPS_BY_MEMBER_ATTRIBUTE"]' \
  -s 'config."preserve.group.inheritance"=["false"]'

# --- MFA: every user must set up and use TOTP -------------------------------------
# The default browser flow only asks for OTP if the user already has one
# ("Condition - user configured"). Make the OTP sub-flow REQUIRED and switch the
# condition off: then everyone without OTP is forced to enrol on first login.
# Send the whole execution back when updating, or Keycloak resets its priority
# and runs OTP *before* the password form.
TMP=$(mktemp -d)
$KCADM get authentication/flows/browser/executions -r darkside > "$TMP/executions.json"
python3 - "$TMP" <<'PY'
import json, sys
tmp = sys.argv[1]
ex = json.load(open(f"{tmp}/executions.json"))
i = next(n for n, e in enumerate(ex) if e.get("displayName") == "Browser - Conditional OTP")
flow, cond = ex[i], ex[i + 1]
assert cond.get("displayName") == "Condition - user configured", cond
flow["requirement"] = "REQUIRED"
cond["requirement"] = "DISABLED"
json.dump(flow, open(f"{tmp}/flow.json", "w"))
json.dump(cond, open(f"{tmp}/cond.json", "w"))
PY
$KCADM update authentication/flows/browser/executions -r darkside -f "$TMP/flow.json"
$KCADM update authentication/flows/browser/executions -r darkside -f "$TMP/cond.json"
rm -rf "$TMP"

# --- OIDC client for the app ------------------------------------------------------
CLIENT_ID=$($KCADM create clients -r darkside -i \
  -s clientId=darkside -s enabled=true -s protocol=openid-connect \
  -s publicClient=false -s "secret=$CLIENT_SECRET" \
  -s standardFlowEnabled=true -s directAccessGrantsEnabled=false \
  -s "redirectUris=[\"$APP_URL/auth/oidc/callback\"]" \
  -s "webOrigins=[\"$APP_URL\"]" \
  -s "attributes.\"post.logout.redirect.uris\"=$APP_URL/login" \
  -s 'attributes."pkce.code.challenge.method"=S256')

$KCADM create "clients/$CLIENT_ID/protocol-mappers/models" -r darkside \
  -s name=groups -s protocol=openid-connect -s protocolMapper=oidc-group-membership-mapper \
  -s 'config."claim.name"=groups' -s 'config."full.path"=false' \
  -s 'config."id.token.claim"=true' -s 'config."access.token.claim"=true' \
  -s 'config."userinfo.token.claim"=true'

echo "Keycloak configured: issuer $KC_URL/realms/darkside, client darkside / $CLIENT_SECRET"
