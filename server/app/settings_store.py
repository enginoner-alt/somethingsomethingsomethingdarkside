"""Typed access to admin-editable settings stored in the settings table."""

from dataclasses import asdict, dataclass, field, fields
from typing import Any, Literal

from sqlalchemy.orm import Session

from app.models import Setting

DIRECTORY_KEY = "directory"
LOGIN_KEY = "login"


@dataclass
class DirectorySettings:
    """Direct Active Directory (LDAP) login, the fallback next to SSO."""

    enabled: bool = False
    # One or more DCs, tried in order: ldaps://dc01.corp.local:636
    servers: list[str] = field(default_factory=list)
    # Only for ldap:// URLs: upgrade to TLS on port 389. Plain LDAP is refused.
    start_tls: bool = False
    # Service account used to look users up (read-only is enough). Its
    # password lives in secrets/ldap_bind_password, never here.
    bind_dn: str = ""
    search_base: str = ""
    username_attribute: str = "sAMAccountName"
    user_filter: str = "(&(objectCategory=person)(objectClass=user))"
    group_filter: str = "(objectClass=group)"
    group_name_attribute: str = "sAMAccountName"
    # AD's LDAP_MATCHING_RULE_IN_CHAIN: groups inside groups count.
    nested_groups: bool = True
    # Exact match on the group's sAMAccountName, or a full DN.
    required_group: str = ""
    admin_group: str = ""
    timeout_seconds: int = 5


@dataclass
class LoginSettings:
    # Used when both SSO and AD work. If only one works, that one is used.
    preferred_method: Literal["oidc", "ldap"] = "oidc"


def _load(db: Session, key: str, cls):
    row = db.get(Setting, key)
    data: dict[str, Any] = dict(row.value) if row else {}
    known = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in data.items() if k in known})


def _save(db: Session, key: str, obj) -> None:
    row = db.get(Setting, key)
    if row is None:
        db.add(Setting(key=key, value=asdict(obj)))
    else:
        row.value = asdict(obj)
    db.commit()


def get_directory(db: Session) -> DirectorySettings:
    return _load(db, DIRECTORY_KEY, DirectorySettings)


def save_directory(db: Session, settings: DirectorySettings) -> None:
    _save(db, DIRECTORY_KEY, settings)


def get_login(db: Session) -> LoginSettings:
    return _load(db, LOGIN_KEY, LoginSettings)


def save_login(db: Session, settings: LoginSettings) -> None:
    _save(db, LOGIN_KEY, settings)
