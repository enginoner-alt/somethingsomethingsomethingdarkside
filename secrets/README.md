# Secrets (one file per secret)

Files here are mounted **read-only into the api container only**, at
`/run/darkside-secrets/`. They never go into the database, the environment,
logs or the admin panel. The panel only shows "set" and a fingerprint that
changes when the value changes.

| File | Used for |
|---|---|
| `ldap_bind_password` | password of the AD service account (Admin → Login methods & Active Directory) |

Create a file with just the value, no quotes, then lock it down and restart the api:

```bash
printf '%s' 'the-password' > secrets/ldap_bind_password
```

```bash
chmod 600 secrets/ldap_bind_password
```

```bash
./darkside app restart api
```

This folder is git-ignored except for this README. Never commit real secrets.
