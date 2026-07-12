"""Opaque, revocable professional user sessions with CSRF defense."""
from __future__ import annotations

import hashlib
import hmac
import secrets
import time
import uuid
from dataclasses import dataclass
from http.cookies import SimpleCookie
from typing import Protocol

from .persistence import Store


class IdentityProvider(Protocol):
    """Optional OIDC/SAML adapter boundary; implementations live in extras."""

    def verify(self, assertion: str) -> tuple[str, str]:
        """Return ``(normalized_email, provider_subject)`` or raise ValueError."""
        ...


def session_token_from_cookie(header: str, name: str = "praxis_session") -> str:
    cookie = SimpleCookie()
    try:
        cookie.load(header or "")
    except Exception:  # malformed client input is unauthenticated
        return ""
    morsel = cookie.get(name)
    return morsel.value if morsel else ""


@dataclass(frozen=True)
class IssuedSession:
    session_id: str
    user_id: str
    organization_id: str
    token: str
    csrf_token: str
    expires_ts: float
    device_id: str


@dataclass(frozen=True)
class AuthenticatedSession:
    session_id: str
    user_id: str
    organization_id: str
    expires_ts: float
    device_id: str


class SessionManager:
    """Issues random bearer secrets while persisting hashes only."""

    def __init__(self, store: Store) -> None:
        self.store = store

    @staticmethod
    def _hash(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    def issue(self, user_id: str, organization_id: str, *,
              ttl_seconds: float = 8 * 3600, device_id: str = "") -> IssuedSession:
        membership = self.store._directory_one(
            "SELECT m.status,u.status AS user_status,o.status AS organization_status "
            "FROM organization_memberships m "
            "JOIN organization_users u ON u.user_id=m.user_id "
            "JOIN organizations o ON o.organization_id=m.organization_id "
            "WHERE m.organization_id=? AND m.user_id=?",
            (organization_id, user_id),
        )
        if (not membership or membership["status"] != "active"
                or membership["user_status"] != "active"
                or membership["organization_status"] != "active"):
            raise ValueError("active organization membership is required")
        token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        now = time.time()
        issued = IssuedSession(
            f"ses-{uuid.uuid4().hex}", user_id, organization_id,
            token, csrf_token, now + ttl_seconds, device_id.strip(),
        )
        self.store._directory_execute(
            "INSERT INTO professional_sessions"
            "(session_id,user_id,organization_id,token_hash,csrf_hash,device_id,"
            "expires_ts,revoked_ts,created_ts) VALUES (?,?,?,?,?,?,?,?,?)",
            (issued.session_id, user_id, organization_id, self._hash(token),
             self._hash(csrf_token), issued.device_id, issued.expires_ts, None, now),
        )
        return issued

    def authenticate(self, token: str, *, mutation: bool = False,
                     csrf_token: str = "") -> AuthenticatedSession | None:
        row = self.store._directory_one(
            "SELECT s.session_id,s.user_id,s.organization_id,s.csrf_hash,"
            "s.expires_ts,s.device_id,s.revoked_ts,m.status AS membership_status,"
            "u.status AS user_status,o.status AS organization_status "
            "FROM professional_sessions s "
            "JOIN organization_memberships m ON m.organization_id=s.organization_id "
            "AND m.user_id=s.user_id JOIN organization_users u ON u.user_id=s.user_id "
            "JOIN organizations o ON o.organization_id=s.organization_id "
            "WHERE s.token_hash=?", (self._hash(token),),
        )
        if not row or row["revoked_ts"] is not None or row["expires_ts"] <= time.time():
            return None
        if any(row[key] != "active" for key in (
                "membership_status", "user_status", "organization_status")):
            return None
        if mutation and not hmac.compare_digest(
                row["csrf_hash"], self._hash(csrf_token)):
            return None
        return AuthenticatedSession(
            row["session_id"], row["user_id"], row["organization_id"],
            row["expires_ts"], row["device_id"],
        )

    def revoke(self, session_id: str) -> bool:
        row = self.store._directory_one(
            "SELECT session_id FROM professional_sessions "
            "WHERE session_id=? AND revoked_ts IS NULL", (session_id,))
        if not row:
            return False
        self.store._directory_execute(
            "UPDATE professional_sessions SET revoked_ts=? WHERE session_id=?",
            (time.time(), session_id),
        )
        return True

    def revoke_device(self, user_id: str, device_id: str) -> int:
        rows = self.store._directory_all(
            "SELECT session_id FROM professional_sessions WHERE user_id=? "
            "AND device_id=? AND revoked_ts IS NULL AND expires_ts>?",
            (user_id, device_id, time.time()),
        )
        now = time.time()
        for row in rows:
            self.store._directory_execute(
                "UPDATE professional_sessions SET revoked_ts=? WHERE session_id=?",
                (now, row["session_id"]),
            )
        return len(rows)
