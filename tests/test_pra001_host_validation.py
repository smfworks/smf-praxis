"""PRA-001 regression: DNS-rebinding Host validation on every request.

The auth gate's Host integrity check (``_request_host_is_loopback``) lived
inside ``_require_auth()``, so public endpoints that skip auth — ``/``,
``/api/auth/status``, ``/api/readiness``, ``/web/*`` — were reachable by a
loopback client sending an attacker-controlled ``Host`` header (DNS rebinding).
A malicious website rebinding to 127.0.0.1 could render the dashboard and read
daemon state without a token.

These tests confirm the Host is now validated on *every* request before route
dispatch, while legitimate loopback access (correct Host) and remote token
access remain unaffected.
"""
from __future__ import annotations

import http.client

import pytest

from hybridagent import config as cfg
from hybridagent import daemon as dmod
from hybridagent.daemon import Daemon, _find_port
from hybridagent.llm import LLMClient


def _start(tmp_path, host="127.0.0.1", port_lo=30400, port_hi=30500):
    port = _find_port(host, port_lo, port_hi)
    d = Daemon(llm=LLMClient(mode="mock"), status_host=host,
               status_port=port, work_dir=str(tmp_path))
    d._start_status_server()
    return d, port


def _get(host: str, port: int, path: str, headers: dict | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path, headers=headers or {})
    resp = conn.getresponse()
    body = resp.read()
    conn.close()
    return resp.status, body


def _post(host: str, port: int, path: str, body: bytes, headers: dict | None = None) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("POST", path, body=body, headers=headers or {})
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


# ---------- DNS-rebinding: public GET endpoints must reject evil Host ----------


@pytest.mark.parametrize("path", ["/", "/api/auth/status", "/api/readiness"])
def test_public_get_rejects_rebinding_host(tmp_path, path):
    """Loopback client + Host: evil.example => 403 on every public endpoint."""
    daemon, port = _start(tmp_path)
    try:
        status, body = _get("127.0.0.1", port, path, headers={"Host": "evil.example"})
        assert status == 403, f"{path} returned {status}: {body!r}"
        assert b"untrusted host" in body
    finally:
        daemon._stop_status_server()


def test_public_get_rejects_rebinding_host_with_port(tmp_path):
    """Host: evil.example:port is still a DNS-rebinding attempt."""
    daemon, port = _start(tmp_path)
    try:
        status, body = _get("127.0.0.1", port, "/api/auth/status",
                            headers={"Host": f"evil.example:{port}"})
        assert status == 403
        assert b"untrusted host" in body
    finally:
        daemon._stop_status_server()


def test_web_static_rejects_rebinding_host(tmp_path):
    """The /web/ static path skips _require_auth but must still check Host."""
    daemon, port = _start(tmp_path)
    try:
        status, body = _get("127.0.0.1", port, "/web/app.js",
                            headers={"Host": "evil.example"})
        # 403 from the Host gate (not 404 from missing file)
        assert status == 403, f"returned {status}: {body!r}"
        assert b"untrusted host" in body
    finally:
        daemon._stop_status_server()


# ---------- DNS-rebinding: POST public route must reject evil Host ----------


def test_login_post_rejects_rebinding_host(tmp_path):
    """The public POST /api/auth/login must reject a rebinding Host."""
    daemon, port = _start(tmp_path)
    try:
        payload = b'{"token": "guess"}'
        status, body = _post("127.0.0.1", port, "/api/auth/login", body=payload,
                             headers={"Host": "evil.example",
                                      "Content-Type": "application/json",
                                      "Content-Length": str(len(payload))})
        assert status == 403
        assert b"untrusted host" in body
    finally:
        daemon._stop_status_server()


# ---------- legitimate access still works (no false positives) ----------


@pytest.mark.parametrize("host_header", ["127.0.0.1", "localhost", None])
def test_loopback_correct_host_still_serves_public(tmp_path, host_header):
    """A loopback Host (or default) must still serve public endpoints."""
    daemon, port = _start(tmp_path)
    try:
        headers = {"Host": f"{host_header}:{port}"} if host_header else {}
        status, body = _get("127.0.0.1", port, "/api/auth/status", headers=headers)
        assert status == 200
        assert b"required" in body
    finally:
        daemon._stop_status_server()


def test_loopback_correct_host_serves_dashboard(tmp_path):
    daemon, port = _start(tmp_path)
    try:
        status, body = _get("127.0.0.1", port, "/",
                            headers={"Host": f"127.0.0.1:{port}"})
        assert status == 200
        assert b"<html" in body.lower()
    finally:
        daemon._stop_status_server()


def test_loopback_gated_api_rejects_rebinding_host(tmp_path):
    """Even gated /api/ routes get the Host check first (defense in depth)."""
    daemon, port = _start(tmp_path)
    try:
        # /api/memory is gated by _require_auth; the Host gate runs before it.
        status, body = _get("127.0.0.1", port, "/api/memory",
                            headers={"Host": "evil.example"})
        assert status == 403
        assert b"untrusted host" in body
    finally:
        daemon._stop_status_server()


def test_loopback_ipv6_loopback_host_accepted(tmp_path):
    """[::1] Host from a loopback client is valid (IPv6 loopback)."""
    daemon, port = _start(tmp_path)
    try:
        status, body = _get("127.0.0.1", port, "/api/auth/status",
                            headers={"Host": f"[::1]:{port}"})
        assert status == 200
    finally:
        daemon._stop_status_server()


# ---------- remote client: Host-exempt, token-gated (no regression) ----------


def test_remote_client_public_status_still_serves(tmp_path, monkeypatch):
    """A remote client (non-loopback) is Host-exempt; public endpoints serve."""
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setattr(dmod._StatusHandler, "_is_loopback", lambda self: False)
    daemon, port = _start(tmp_path, host="0.0.0.0")
    try:
        status, body = _get("127.0.0.1", port, "/api/auth/status",
                            headers={"Host": f"10.0.0.5:{port}"})
        assert status == 200
    finally:
        daemon._stop_status_server()