# AD test harness (Samba AD DC → app)

**Test harness only. Not part of the POC or production deployment.**

Tests the app's direct Active Directory login against a real AD-compatible
domain controller (Samba 4 AD DC): same LDAP schema as Windows AD
(`sAMAccountName`, `objectGUID`, `userAccountControl`, `member`/`memberOf`),
LDAPS and StartTLS, and nested-group queries (`LDAP_MATCHING_RULE_IN_CHAIN`).

## Test domain (seed-samba.sh)

Domain `CORP.TEST`, DC `dc01.corp.test`. All passwords `Passw0rd-Test!`.

| Account | Groups | Expected |
|---|---|---|
| `svc-darkside` | | the service account the app binds with |
| `jdoe` | darkside-admins, darkside-users | logs in, admin |
| `asmith` | darkside-users | logs in, not admin |
| `nina` | eng → darkside-users (nested) | logs in through the nested group |
| `dave` | darkside-users, **disabled in AD** | refused |
| `mallory` | decoy group only | refused |

The decoy group sits in `OU=Helpdesk` with **CN `darkside-admins`** but
sAMAccountName `helpdesk-admins`. It must never grant admin.

## What e2e_ad.py checks (26 checks, all passing in the sandbox)

- Admin page: the secret shows as "set" with a fingerprint, and the value never appears.
- Verify AD: each DC (certificate verified, service account bound), the search
  base, a test user's groups (nested), required/admin group, the test password,
  a disabled user, and the decoy group not counting.
- Login policy: SSO up and preferred → AD password refused. SSO down → notice
  shown and AD logins work. AD preferred → AD login even with SSO up.
- Users: admin / not admin / nested / disabled / decoy / wrong password /
  empty password / LDAP filter characters in the username.
- First DC dead → reported by Verify, and login fails over to the second DC.
- Plain `ldap://` refused. StartTLS on 389 works.
- Disabled in the app → AD login refused.

"SSO up/down" uses a fake IdP that only serves the discovery document. A full
SSO login federated to this AD (with the objectGUID claim linking the
accounts) is **not** covered here. The linking logic itself is covered by unit
tests.

## Running it (Ubuntu)

```bash
sudo apt install samba
```

On Ubuntu with Python 3.13 as default, run `samba-tool` with python3.12:

```bash
sudo python3.12 /usr/bin/samba-tool domain provision --realm=CORP.TEST --domain=CORP --server-role=dc --dns-backend=SAMBA_INTERNAL --host-name=dc01 --adminpass='Adm1n-Test-Passw0rd!'
```

Give the DC a proper certificate (Samba's auto-generated one lacks the
Authority Key Identifier, which Python 3.13+ rejects; real AD CS certificates
have it). Make a chain with `../make-test-certs.sh` (`DOMAIN=corp.test`), copy
the files into `/var/lib/samba/private/tls/`, and set `tls keyfile`,
`tls certfile` and `tls cafile` in `/etc/samba/smb.conf`. Then:

```bash
sudo samba -D && sudo ./seed-samba.sh
```

Run the app with `SECRETS_DIR` holding `ldap_bind_password` (`Passw0rd-Test!`),
`EXTRA_CA_DIR` holding the test `ca.pem`, and
`OIDC_ISSUER=http://127.0.0.1:18090/realms/test`. Then:

```bash
python e2e_ad.py
```
