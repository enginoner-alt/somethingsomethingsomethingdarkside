"""Secrets read from files (secrets/ on the host, mounted read-only).

One file per secret, e.g. secrets/ldap_bind_password. Values are never stored
in the database, environment, logs or shown in the admin panel; the panel
only shows whether a secret is set and a fingerprint that changes when the
value changes.
"""

import hashlib
import hmac
from pathlib import Path

from app.config import get_config

LDAP_BIND_PASSWORD = "ldap_bind_password"
# In its own subfolder so only it is mounted into worker-llm (not the AD password).
LLM_API_KEY = "llm/llm_api_key"


def read_secret(name: str) -> str | None:
    path = Path(get_config().secrets_dir) / name
    try:
        value = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, IsADirectoryError, PermissionError):
        return None
    return value or None


def fingerprint(value: str) -> str:
    """Keyed with SECRET_KEY, so the fingerprint can't be used to guess the
    secret offline; it only shows that the value changed."""
    key = get_config().secret_key.encode()
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()[:12]
