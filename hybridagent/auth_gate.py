"""Built-in session auth for non-loopback dashboard access.

When the daemon is reachable beyond loopback, mutating API calls require a
shared token (``PRAXIS_AUTH_TOKEN`` env, or ``agents.auth.token`` in config).
Loopback clients skip the check so local single-user use stays frictionless.

The browser stores the token in ``sessionStorage`` after a one-time login form;
every subsequent ``fetch`` sends ``Authorization: Bearer <token>`` or
``X-Praxis-Token``.
"""
from __future__ import annotations

import hmac
import os
import secrets

from . import config as cfg

_ENV_TOKEN = "PRAXIS_AUTH_TOKEN"


def configured_token() -> str:
    """Return the active shared token, or empty if auth is not configured."""
    env = (os.environ.get(_ENV_TOKEN) or "").strip()
    if env:
        return env
    block = (cfg.load_config().get("agents") or {}).get("auth") or {}
    return str(block.get("token") or "").strip()


def ensure_token() -> str:
    """Return a token, minting and persisting one if none is configured.

    Called when binding beyond loopback so operators are never left open by
    accident. The minted token is written to config (not env).
    """
    existing = configured_token()
    if existing:
        return existing
    token = secrets.token_urlsafe(32)
    conf = cfg.load_config()
    agents = conf.setdefault("agents", {})
    auth = agents.setdefault("auth", {})
    auth["token"] = token
    auth["minted"] = True
    cfg.save_config(conf)
    return token


def auth_required(bind_host: str) -> bool:
    """True when the bind address is not loopback-only."""
    host = (bind_host or "127.0.0.1").strip().lower()
    if host in ("127.0.0.1", "::1", "localhost"):
        return False
    # 0.0.0.0 / :: / LAN IPs require auth when a token is (or will be) set.
    return True


def token_matches(provided: str | None) -> bool:
    expected = configured_token()
    if not expected:
        return True  # auth not configured → open (loopback-safe default)
    got = (provided or "").strip()
    if not got:
        return False
    # Constant-time compare of raw token (length mismatch returns False).
    return hmac.compare_digest(expected, got)


def extract_token(headers: dict | object) -> str:
    """Pull a bearer / X-Praxis-Token from a headers mapping or BaseHTTP headers."""
    def _get(name: str) -> str:
        if hasattr(headers, "get"):
            return str(headers.get(name) or headers.get(name.lower()) or "")
        return ""
    auth = _get("Authorization") or _get("authorization")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (_get("X-Praxis-Token") or _get("x-praxis-token")).strip()


def status_dict(bind_host: str) -> dict:
    tok = configured_token()
    required = auth_required(bind_host)
    return {
        "required": required and bool(tok),
        "configured": bool(tok),
        "bind_host": bind_host,
        "hint": (
            "Send Authorization: Bearer <token> or X-Praxis-Token. "
            "Token from PRAXIS_AUTH_TOKEN or agents.auth.token."
            if tok else
            "No token configured; non-loopback binds should set PRAXIS_AUTH_TOKEN."
        ),
    }
