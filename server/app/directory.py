"""Direct Active Directory (LDAP) login: the fallback next to SSO.

Flow for a login attempt:
  1. bind as the service account (password from secrets/ldap_bind_password)
  2. find exactly one user by sAMAccountName (escaped), refuse if disabled
  3. bind as that user with the typed password: AD checks the password
  4. read the user's groups (nested via LDAP_MATCHING_RULE_IN_CHAIN)
Only LDAPS or StartTLS; plain LDAP is refused. Certificates are verified
against public CAs plus deploy/ca/.
"""

import base64
import logging
import ssl
import uuid
from dataclasses import dataclass, field
from typing import Any

from ldap3 import FIRST, SUBTREE, Connection, Server, ServerPool, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars

from app import secrets
from app.settings_store import DirectorySettings
from app.tls import ca_bundle_path

log = logging.getLogger("directory")

UAC_ACCOUNTDISABLE = 0x2
IN_CHAIN = "1.2.840.113556.1.4.1941"  # LDAP_MATCHING_RULE_IN_CHAIN


class DirectoryUnavailable(Exception):
    """Can't reach or bind to AD (network, TLS, service account)."""


@dataclass
class DirectoryUser:
    guid: str
    username: str
    dn: str
    display_name: str | None
    disabled: bool
    groups: set[str] = field(default_factory=set)


def normalize_guid(value: Any) -> str | None:
    """AD objectGUID in any common form -> lowercase GUID string.

    Accepts "{F22CF220-...}", "f22cf220-...", raw 16 bytes (objectGUID's
    little-endian layout), or base64 of those bytes (Entra's
    onPremisesImmutableId)."""
    if value is None:
        return None
    if isinstance(value, bytes | bytearray):
        return str(uuid.UUID(bytes_le=bytes(value))) if len(value) == 16 else None
    text = str(value).strip().strip("{}")
    try:
        return str(uuid.UUID(text))
    except ValueError:
        pass
    try:
        raw = base64.b64decode(text, validate=True)
    except ValueError:
        return None
    return str(uuid.UUID(bytes_le=raw)) if len(raw) == 16 else None


def config_problems(s: DirectorySettings) -> list[str]:
    """Why these settings can't work, before touching the network.
    Translation keys: `dir.problem.<key>`."""
    problems = []
    if not s.servers:
        problems.append("no_server")
    for url in s.servers:
        if url.startswith("ldaps://"):
            continue
        if url.startswith("ldap://") and s.start_tls:
            continue
        problems.append("plain_ldap")
        break
    if not s.bind_dn:
        problems.append("no_bind_dn")
    if not s.search_base:
        problems.append("no_search_base")
    if secrets.read_secret(secrets.LDAP_BIND_PASSWORD) is None:
        problems.append("no_bind_password")
    return problems


def is_configured(s: DirectorySettings) -> bool:
    return s.enabled and not config_problems(s)


class Directory:
    def __init__(self, settings: DirectorySettings):
        self.s = settings
        tls = Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=ca_bundle_path())
        servers = [
            Server(
                url,
                use_ssl=url.startswith("ldaps://"),
                tls=tls,
                connect_timeout=settings.timeout_seconds,
            )
            for url in settings.servers
        ]
        # FIRST: try DCs in the given order; active: skip one that's down.
        self.pool = ServerPool(servers, FIRST, active=1, exhaust=True)

    def _connect(self, user: str, password: str) -> Connection:
        conn = Connection(
            self.pool,
            user=user,
            password=password,
            receive_timeout=self.s.timeout_seconds,
            raise_exceptions=False,
            read_only=True,
        )
        conn.open()
        if self.s.start_tls and not conn.server.ssl:
            conn.start_tls()
        return conn

    def service_connection(self) -> Connection:
        password = secrets.read_secret(secrets.LDAP_BIND_PASSWORD)
        if not password:
            raise DirectoryUnavailable("secrets/ldap_bind_password is missing")
        try:
            conn = self._connect(self.s.bind_dn, password)
            if not conn.bind():
                raise DirectoryUnavailable(
                    f"service account bind failed: {conn.result.get('description')}"
                )
        except LDAPException as exc:
            raise DirectoryUnavailable(f"{exc.__class__.__name__}: {exc}") from exc
        return conn

    def find_user(self, conn: Connection, username: str) -> DirectoryUser | None:
        attr = self.s.username_attribute
        flt = f"(&{self.s.user_filter}({attr}={escape_filter_chars(username)}))"
        attrs = [
            attr,
            "objectGUID",
            "distinguishedName",
            "displayName",
            "givenName",
            "sn",
            "userAccountControl",
        ]
        conn.search(self.s.search_base, flt, SUBTREE, attributes=attrs, size_limit=2)
        entries = [e for e in conn.entries if "objectGUID" in e]
        if len(entries) != 1:
            return None  # none, or ambiguous: never guess
        e = entries[0]
        guid = normalize_guid(e.objectGUID.raw_values[0] if e.objectGUID.raw_values else None)
        if guid is None:
            return None
        uac = (
            int(e.userAccountControl.value)
            if "userAccountControl" in e and e.userAccountControl.value
            else 0
        )
        given = e.givenName.value if "givenName" in e else None
        sn = e.sn.value if "sn" in e else None
        display = (e.displayName.value if "displayName" in e else None) or (
            " ".join(p for p in (given, sn) if p) or None
        )
        user = DirectoryUser(
            guid=guid,
            username=str(e[attr].value),
            dn=e.entry_dn,
            display_name=display,
            disabled=bool(uac & UAC_ACCOUNTDISABLE),
        )
        user.groups = self.groups_of(conn, user.dn)
        return user

    def groups_of(self, conn: Connection, user_dn: str) -> set[str]:
        """Group names (sAMAccountName) and DNs, so a setting can be either."""
        member = f"member:{IN_CHAIN}:" if self.s.nested_groups else "member"
        flt = f"(&{self.s.group_filter}({member}={escape_filter_chars(user_dn)}))"
        name_attr = self.s.group_name_attribute
        conn.search(self.s.search_base, flt, SUBTREE, attributes=[name_attr])
        groups: set[str] = set()
        for e in conn.entries:
            groups.add(e.entry_dn)
            if name_attr in e and e[name_attr].value:
                groups.add(str(e[name_attr].value))
        return groups

    def check_password(self, user_dn: str, password: str) -> bool:
        # An empty password would be an "unauthenticated bind", which AD
        # answers with success. Never send one.
        if not password:
            return False
        try:
            conn = self._connect(user_dn, password)
            ok = conn.bind()
            conn.unbind()
            return bool(ok)
        except LDAPException as exc:
            raise DirectoryUnavailable(f"{exc.__class__.__name__}: {exc}") from exc

    def authenticate(self, username: str, password: str) -> DirectoryUser | None:
        """The AD user if username + password are right and the account is
        enabled; None otherwise. Raises DirectoryUnavailable if AD can't be
        asked at all."""
        if not username or not password or len(username) > 256:
            return None
        conn = self.service_connection()
        try:
            user = self.find_user(conn, username)
        except LDAPException as exc:
            raise DirectoryUnavailable(f"{exc.__class__.__name__}: {exc}") from exc
        finally:
            conn.unbind()
        if user is None or user.disabled:
            return None
        if not self.check_password(user.dn, password):
            return None
        return user


@dataclass
class Step:
    """One line of a Verify result. `label` and `note` are translation keys
    (`step.<label>`, `step.note.<note>`, filled from `params`); `detail` is
    technical text shown as-is (URLs, DNs, server messages)."""

    label: str
    ok: bool
    detail: str = ""
    note: str = ""
    params: dict = field(default_factory=dict)


def verify(s: DirectorySettings, test_username: str = "", test_password: str = "") -> list[Step]:
    """Admin "Verify AD" button: every step, reported, nothing stored.

    Each DC is tried on its own so a broken one shows up even when another
    works. With a test username, also shows what a login would see (groups,
    required/admin match); with a test password, checks that password too."""
    from app.oidc import group_matches  # avoid import cycle at module load

    steps: list[Step] = []
    problems = config_problems(s)
    steps.append(Step("settings", not problems, note="incomplete" if problems else "complete"))
    if problems:
        return steps

    working: Directory | None = None
    for url in s.servers:
        single = Directory(DirectorySettings(**{**s.__dict__, "servers": [url]}))
        try:
            conn = single.service_connection()
            tls = "LDAPS" if conn.server.ssl else "StartTLS"
            conn.unbind()
            steps.append(Step("dc", True, url, note="dc_ok", params={"url": url, "tls": tls}))
            working = working or single
        except DirectoryUnavailable as exc:
            steps.append(Step("dc", False, str(exc), params={"url": url}))
    if working is None:
        return steps

    conn = working.service_connection()
    try:
        ok = conn.search(s.search_base, s.user_filter, SUBTREE, attributes=["cn"], size_limit=1)
        steps.append(
            Step(
                "search_base",
                bool(ok) or conn.result.get("description") == "sizeLimitExceeded",
                f"{s.search_base}: {conn.result.get('description')}",
            )
        )
        if not test_username:
            return steps
        try:
            user = working.find_user(conn, test_username.strip())
        except LDAPException as exc:
            steps.append(Step("find_user", False, str(exc), params={"username": test_username}))
            return steps
    finally:
        conn.unbind()

    if user is None:
        steps.append(
            Step("find_user", False, note="user_not_found", params={"username": test_username})
        )
        return steps
    steps.append(
        Step(
            "find_user",
            True,
            f"{user.dn} (objectGUID {user.guid})",
            params={"username": test_username},
        )
    )
    steps.append(
        Step("account_enabled", not user.disabled, note="disabled" if user.disabled else "yes")
    )
    names = sorted(g for g in user.groups if "=" not in g)
    steps.append(Step("groups", True, ", ".join(names), note="" if names else "none"))
    if s.required_group:
        hit = group_matches(s.required_group, user.groups)
        steps.append(
            Step(
                "required_group",
                hit,
                note="member" if hit else "not_member",
                params={"group": s.required_group},
            )
        )
    if s.admin_group:
        hit = group_matches(s.admin_group, user.groups)
        steps.append(
            Step(
                "admin_group",
                True,
                note="admin" if hit else "not_admin",
                params={"group": s.admin_group},
            )
        )
    if test_password:
        good = working.check_password(user.dn, test_password)
        steps.append(Step("test_password", good, note="pw_accepted" if good else "pw_rejected"))
    return steps
