import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

_hasher = PasswordHasher()

# Used when the user doesn't exist, so login timing doesn't reveal usernames.
_DUMMY_HASH = _hasher.hash("not-a-real-password")


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, InvalidHashError):
        return False


CSRF_SESSION_KEY = "csrf"


def get_csrf_token(session: dict) -> str:
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def check_csrf(session: dict, submitted: str | None) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    return bool(expected and submitted and hmac.compare_digest(expected, submitted))
