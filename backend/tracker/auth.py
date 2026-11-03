"""Authentication and authorization: users, sessions, personal tokens, roles, rate limits and the audit log.

Off by default (`TRACKER_AUTH=off`) for local research: the dashboard then warns that the instance must not be
reachable from the internet. With `TRACKER_AUTH=on` every API route needs a signed-in session (an HttpOnly cookie
set by POST /api/auth/login) or a personal token (`Authorization: Bearer trk_…`), except /api/health,
/api/auth/status, /api/auth/login and the provider webhook (which has its own secret). Roles, each including the
previous one:

- viewer: reads everything;
- analyst: also operates (syncs, watchlist, seeds, imports, backtests, marking alerts read);
- admin: also manages users and reads the audit log.

Passwords are hashed with scrypt (N=2^15, r=8, p=1, random 16-byte salt). Session and token values are random
256-bit strings; only their SHA-256 is stored. Five failed sign-ins lock an account for 15 minutes. Sign-in attempts
are limited per client address and requests per principal (in memory, per API process). Every state-changing
request is written to the audit log with its user, status and client address. The first admin is created from the
command line: `python -m tracker.users create USERNAME --role admin`.
"""

import base64
import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update

from .db import ApiToken, AuditEvent, User, UserSession, now_iso

ROLES = {"viewer": 0, "analyst": 1, "admin": 2}
ROLE_NAMES = {"viewer": "lector", "analyst": "analista", "admin": "administrador"}
SESSION_COOKIE = "tracker_session"
SESSION_IDLE = timedelta(hours=12)
SESSION_MAX = timedelta(days=7)
LOCK_AFTER, LOCK_FOR = 5, timedelta(minutes=15)
MIN_PASSWORD = 12
USERNAME = re.compile(r"^[a-zA-Z0-9_.-]{3,64}$")
TOKEN_PREFIX = "trk_"
SCRYPT = {"n": 2 ** 15, "r": 8, "p": 1}
PUBLIC = {("GET", "/api/health"), ("GET", "/api/auth/status"), ("POST", "/api/auth/login")}


def enabled() -> bool:
    return os.getenv("TRACKER_AUTH", "off").strip().lower() in ("on", "1", "true", "yes")


def cookie_secure() -> bool:
    return os.getenv("TRACKER_COOKIE_SECURE", "1") != "0"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, maxmem=64 * 1024 * 1024, dklen=32, **SCRYPT)
    return f"scrypt${SCRYPT['n']}${SCRYPT['r']}${SCRYPT['p']}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=int(n), r=int(r), p=int(p),
                                maxmem=64 * 1024 * 1024, dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, base64.b64decode(digest))


_DUMMY: list[str] = []


def _dummy_hash() -> str:
    """A hash to verify against for unknown usernames, so they take as long as known ones."""
    if not _DUMMY:
        _DUMMY.append(hash_password(secrets.token_urlsafe(16)))
    return _DUMMY[0]


def password_problem(password: str, username: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"La contraseña debe tener al menos {MIN_PASSWORD} caracteres"
    if len(password) > 256:
        return "La contraseña es demasiado larga"
    if username and username.lower() in password.lower():
        return "La contraseña no puede contener el nombre de usuario"
    return None


def username_problem(username: str) -> str | None:
    if not USERNAME.match(username):
        return "Usuario: 3-64 caracteres entre letras, números, punto, guion y guion bajo"
    return None


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Principal:
    id: int
    username: str
    role: str
    via: str  # session | token
    token: str | None = None  # the session cookie value, for sign-out

    def can(self, role: str) -> bool:
        return ROLES[self.role] >= ROLES[role]


def authenticate(session, username: str, password: str) -> User | None:
    """The user if the password matches an active, unlocked account; records failures and lockouts."""
    user = session.scalar(select(User).where(User.username == username))
    if user is None:
        verify_password(password, _dummy_hash())
        return None
    now = _now()
    if user.locked_until and datetime.fromisoformat(user.locked_until) > now:
        verify_password(password, _dummy_hash())
        return None
    if not verify_password(password, user.password_hash) or not user.active:
        user.failed_logins += 1
        if user.failed_logins >= LOCK_AFTER:
            user.locked_until, user.failed_logins = (now + LOCK_FOR).isoformat(), 0
        session.commit()
        return None
    user.failed_logins, user.locked_until, user.last_login_at = 0, None, now.isoformat()
    session.commit()
    return user


def create_session(session, user: User) -> str:
    token = secrets.token_urlsafe(32)
    session.add(UserSession(token_hash=digest(token), user_id=user.id, expires_at=(_now() + SESSION_MAX).isoformat()))
    session.commit()
    return token


def session_user(session, token: str) -> User | None:
    row = session.get(UserSession, digest(token))
    if row is None:
        return None
    now = _now()
    if datetime.fromisoformat(row.expires_at) <= now or datetime.fromisoformat(row.last_seen_at) + SESSION_IDLE <= now:
        session.delete(row)
        session.commit()
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.active:
        return None
    if (now - datetime.fromisoformat(row.last_seen_at)).total_seconds() > 60:
        row.last_seen_at = now.isoformat()
        session.commit()
    return user


def end_sessions(session, user_id: int, *, keep: str | None = None) -> int:
    rows = list(session.scalars(select(UserSession).where(UserSession.user_id == user_id)))
    removed = 0
    for row in rows:
        if keep is None or row.token_hash != digest(keep):
            session.delete(row)
            removed += 1
    session.commit()
    return removed


def end_session(session, token: str):
    row = session.get(UserSession, digest(token))
    if row is not None:
        session.delete(row)
        session.commit()


def create_api_token(session, user: User, name: str) -> tuple[ApiToken, str]:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(token_hash=digest(token), user_id=user.id, name=name)
    session.add(row)
    session.commit()
    return row, token


def token_user(session, token: str) -> User | None:
    if not token.startswith(TOKEN_PREFIX):
        return None
    row = session.scalar(select(ApiToken).where(ApiToken.token_hash == digest(token)))
    if row is None or row.revoked:
        return None
    user = session.get(User, row.user_id)
    if user is None or not user.active:
        return None
    now = now_iso()
    if row.last_used_at is None or row.last_used_at[:16] != now[:16]:  # at most once a minute
        row.last_used_at = now
        session.commit()
    return user


def resolve(session, *, cookie: str | None, authorization: str | None) -> Principal | None:
    if authorization and authorization.lower().startswith("bearer "):
        user = token_user(session, authorization[7:].strip())
        return Principal(user.id, user.username, user.role, "token") if user else None
    if cookie:
        user = session_user(session, cookie)
        return Principal(user.id, user.username, user.role, "session", cookie) if user else None
    return None


def required_role(method: str, path: str) -> str | None:
    """Minimum role for a request; None for public routes. New routes default to: reads viewer, writes analyst."""
    if method == "OPTIONS" or (method, path) in PUBLIC or path.startswith("/api/hooks/"):
        return None
    if path.startswith(("/api/users", "/api/audit")):
        return "admin"
    if path.startswith("/api/auth/") or method in ("GET", "HEAD"):
        return "viewer"
    return "analyst"


def active_admins(session) -> int:
    return session.scalar(select(func.count()).select_from(User).where(User.role == "admin", User.active.is_(True)))


def revoke_tokens(session, user_id: int):
    session.execute(update(ApiToken).where(ApiToken.user_id == user_id).values(revoked=True))
    session.commit()


def audit(session, username: str | None, action: str, *, status: int | None = None, ip: str | None = None,
          details: dict | None = None):
    session.add(AuditEvent(username=username, action=action[:120], status=status, ip=ip, details=details))
    session.commit()


def user_json(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role, "active": u.active, "created_at": u.created_at,
            "last_login_at": u.last_login_at, "locked": bool(u.locked_until and datetime.fromisoformat(
                u.locked_until) > _now())}


class RateLimiter:
    """Sliding-window counter per key (in memory, per process)."""

    def __init__(self):
        self.lock, self.hits = threading.Lock(), {}

    def allow(self, key: str, limit: int, window: float = 60.0) -> tuple[bool, int]:
        now = time.monotonic()
        with self.lock:
            hits = self.hits.setdefault(key, deque())
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(1, int(hits[0] + window - now) + 1)
            hits.append(now)
            if len(self.hits) > 10_000:  # drop idle keys
                self.hits = {k: v for k, v in self.hits.items() if v and v[-1] > now - window}
            return True, 0


def request_limit() -> int:
    return int(os.getenv("TRACKER_RATE_LIMIT_PER_MINUTE", "600"))


LOGIN_LIMIT_PER_MINUTE = 10
