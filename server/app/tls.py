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
