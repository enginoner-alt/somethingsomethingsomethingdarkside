#!/bin/sh
# Seed a throwaway Samba AD domain (CORP.TEST) for testing the AD login.
# TEST DATA ONLY. All user passwords: "Passw0rd-Test!"
#
#   CN=Users:  svc-darkside  service account the app binds with
#              jdoe          member of darkside-admins (and darkside-users)
#              asmith        member of darkside-users
#              nina          member of eng, which is a member of darkside-users (nested)
#              dave          member of darkside-users but DISABLED
#              mallory       member of the decoy group only
#   OU=Helpdesk: a decoy group whose CN is also "darkside-admins" but whose
#              sAMAccountName is "helpdesk-admins" (must NOT grant admin)
set -eu
ST=${SAMBA_TOOL:-"python3.12 /usr/bin/samba-tool"}
PW='Passw0rd-Test!'

for u in svc-darkside jdoe asmith nina dave mallory; do
  $ST user delete "$u" >/dev/null 2>&1 || true
done
for g in darkside-admins darkside-users eng helpdesk-admins; do
  $ST group delete "$g" >/dev/null 2>&1 || true
done
$ST ou delete "OU=Helpdesk" --force-subtree-delete >/dev/null 2>&1 || true

$ST user create svc-darkside "$PW" --description="Darkside LDAP bind (read-only)" >/dev/null
$ST user create jdoe    "$PW" --given-name=Jane    --surname=Doe     >/dev/null
$ST user create asmith  "$PW" --given-name=Alan    --surname=Smith   >/dev/null
$ST user create nina    "$PW" --given-name=Nina    --surname=Nested  >/dev/null
$ST user create dave    "$PW" --given-name=Dave    --surname=Disabled >/dev/null
$ST user create mallory "$PW" --given-name=Mallory --surname=Outsider >/dev/null
$ST user disable dave >/dev/null

$ST group add darkside-users  >/dev/null
$ST group add darkside-admins >/dev/null
$ST group add eng             >/dev/null
$ST ou add "OU=Helpdesk" >/dev/null
# Same CN as the admin group, different sAMAccountName, different OU.
$ST group add helpdesk-admins --groupou=OU=Helpdesk >/dev/null
$ST group rename helpdesk-admins --force-new-cn=darkside-admins >/dev/null 2>&1 || true

$ST group addmembers darkside-users  jdoe,asmith,dave,eng >/dev/null
$ST group addmembers darkside-admins jdoe >/dev/null
$ST group addmembers eng             nina >/dev/null
$ST group addmembers helpdesk-admins mallory >/dev/null

echo "Samba AD seeded (CORP.TEST)"
