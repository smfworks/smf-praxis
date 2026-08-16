"""PRA-002 regression: all /api/ GET routes must honor the shared token.

The auth gate previously protected only mutating routes (POST/PUT/DELETE).
Every GET endpoint — knowledge base, pending approvals, task history, audit
log, daemon state, secrets status — answered unauthenticated, even on
non-loopback binds. These tests confirm the GET gate is now in place while
preserving the loopback frictionless default and the public auth-status
discovery endpoint.
"""

import json
import socket
import urllib.error
import urllib.request

import pytest

from hybridagent import config as cfg
from hybridagent.daemon import Daemon, _StatusHandler
from hybridagent.llm import LLMClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, json.loads(r.read().decode() or "null")


def _get_error(url, headers=None):
    """Return (status, body) for a request expected to fail with HTTPError."""
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


@pytest.fixture
def remote_daemon(tmp_path, monkeypatch):
    """A daemon bound beyond loopback with a shared token configured.

    ``_is_loopback`` is patched to False so the handler treats every
    connection as remote — this is the PRA-002 threat model: a non-loopback
    client reaching the read APIs without a token.
    """
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "s3cret-token-xyz")
    # Simulate a non-loopback client on every request handled by this server.
    monkeypatch.setattr(_StatusHandler, "_is_loopback", lambda self: False)

    daemon = Daemon(
        llm=LLMClient(mode="mock"),
        status_host="0.0.0.0",
        status_port=_free_port(),
    )
    daemon._start_status_server()
    try:
        yield f"http://127.0.0.1:{daemon.status_port}", daemon
    finally:
        daemon._stop_status_server()
    # Clear the env token so it does not leak into other tests in the session.
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)


# ---------- endpoints that must now require a token ----------


@pytest.mark.parametrize(
    "route",
    [
        "/api/tasks",
        "/api/approvals",
        "/api/audit",
        "/api/secrets",
        "/api/memory",
        "/api/metrics",
        "/api/model",
        "/api/providers",
        "/api/sources",
        "/api/cron",
        "/api/killswitch",
        "/api/compliance",
        "/api/board",
        "/api/traces",
        "/api/persona",
    ],
)
def test_get_api_route_rejects_missing_token(remote_daemon, route):
    """No token → 401 for every previously-open GET read endpoint."""
    base, _ = remote_daemon
    status, body = _get_error(f"{base}{route}")
    assert status == 401, f"{route} returned {status}: {body}"
    assert "auth_required" in body


def test_get_api_route_rejects_wrong_token(remote_daemon):
    base, _ = remote_daemon
    status, body = _get_error(
        f"{base}/api/audit",
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert status == 401
    assert "auth_required" in body


def test_get_api_route_accepts_bearer_token(remote_daemon):
    base, _ = remote_daemon
    status, data = _get(
        f"{base}/api/audit",
        headers={"Authorization": "Bearer s3cret-token-xyz"},
    )
    assert status == 200
    assert isinstance(data, dict)


def test_get_api_route_accepts_x_praxis_token(remote_daemon):
    base, _ = remote_daemon
    status, _ = _get(
        f"{base}/api/tasks",
        headers={"X-Praxis-Token": "s3cret-token-xyz"},
    )
    assert status == 200


# ---------- endpoints that must stay public ----------


def test_auth_status_remains_public(remote_daemon):
    """The login form needs auth status before the browser has a token."""
    base, _ = remote_daemon
    status, data = _get(f"{base}/api/auth/status")
    assert status == 200
    assert data["required"] is True


def test_dashboard_html_remains_public(remote_daemon):
    """The login page itself must render so the user can authenticate."""
    base, _ = remote_daemon
    req = urllib.request.Request(f"{base}/")
    with urllib.request.urlopen(req, timeout=10) as r:
        assert r.status == 200
        assert b"<html" in r.read().lower()


# ---------- loopback frictionless default (no regression) ----------


def test_loopback_get_without_token_still_works(tmp_path, monkeypatch):
    """Loopback binds with no token configured stay frictionless."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    # Ensure no persisted token from a prior run leaks into config.
    from hybridagent import auth_gate

    monkeypatch.setattr(auth_gate, "configured_token", lambda: "")

    daemon = Daemon(
        llm=LLMClient(mode="mock"),
        status_host="127.0.0.1",
        status_port=_free_port(),
    )
    daemon._start_status_server()
    try:
        base = f"http://127.0.0.1:{daemon.status_port}"
        status, data = _get(f"{base}/api/auth/status")
        assert status == 200
        assert data["required"] is False
        # A read endpoint on loopback with no token must still succeed.
        status, _ = _get(f"{base}/api/tasks")
        assert status == 200
    finally:
        daemon._stop_status_server()
