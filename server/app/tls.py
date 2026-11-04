"""Which certificate authorities the app trusts for its own outgoing HTTPS
(e.g. talking to Keycloak, later the LLM endpoint).

Public CAs are trusted out of the box. For an internal or self-signed CA (AD
Certificate Services, a homelab CA, ...), drop its certificate as a .pem/.crt
file into deploy/ca/ — it is mounted at EXTRA_CA_DIR and picked up on start.
No other setting needed.
"""

import logging
import ssl
from functools import lru_cache
from pathlib import Path

import certifi

from app.config import get_config

log = logging.getLogger("tls")


@lru_cache
def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    ca_dir = Path(get_config().extra_ca_dir)
    if ca_dir.is_dir():
        for path in sorted(ca_dir.iterdir()):
            if path.suffix.lower() in {".pem", ".crt", ".cer"} and path.is_file():
                try:
                    ctx.load_verify_locations(cafile=str(path))
                    log.info("trusting extra CA certificate %s", path.name)
                except ssl.SSLError as exc:
                    log.error("ignoring %s: not a valid PEM certificate (%s)", path.name, exc)
    return ctx


@lru_cache
def ca_bundle_path() -> str:
    """The same trust (public CAs + deploy/ca/) as a PEM file, for libraries
    that want a file instead of an SSLContext (ldap3)."""
    import tempfile

    parts = [Path(certifi.where()).read_text(encoding="utf-8")]
    ca_dir = Path(get_config().extra_ca_dir)
    if ca_dir.is_dir():
        for path in sorted(ca_dir.iterdir()):
            if path.suffix.lower() in {".pem", ".crt", ".cer"} and path.is_file():
                parts.append(path.read_text(encoding="utf-8", errors="replace"))
    fd, name = tempfile.mkstemp(prefix="darkside-ca-", suffix=".pem")
    with open(fd, "w", encoding="utf-8") as fh:
        fh.write("\n".join(parts))
    return name
