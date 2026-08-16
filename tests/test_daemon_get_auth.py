"""GET control-plane must honor Host integrity and remote token."""

from __future__ import annotations

import http.client
import json

from hybridagent.daemon import Daemon, _find_port
from hybridagent.llm import LLMClient


def _start(tmp_path, host="127.0.0.1"):
    port = _find_port(host, 30000, 30100)
    d = Daemon(
        llm=LLMClient(mode="mock"), status_host=host, status_port=port, work_dir=str(tmp_path)
    )
    d._start_status_server()
    return d, port


def test_get_api_memory_rejects_non_loopback_host(tmp_path):
    daemon, port = _start(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/memory", headers={"Host": "evil.example:8643"})
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status == 403, body
        assert b"untrusted host" in body
        conn.close()
    finally:
        daemon._stop_status_server()


def test_get_api_memory_allows_loopback_host(tmp_path):
    daemon, port = _start(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/memory", headers={"Host": f"127.0.0.1:{port}"})
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        conn.close()
    finally:
        daemon._stop_status_server()


def test_get_status_rejects_rebinding_host(tmp_path):
    daemon, port = _start(tmp_path)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/status", headers={"Host": "evil.example"})
        resp = conn.getresponse()
        assert resp.status == 403, resp.read()
        conn.close()
    finally:
        daemon._stop_status_server()


def test_public_endpoints_reject_rebinding_host(tmp_path):
    """PRA-001: public endpoints must reject a rebound (foreign) Host.

    /api/auth/status and /api/readiness are intentionally public (no token),
    but a DNS-rebinding browser sends them with a foreign Host from a loopback
    peer. The universal Host gate must 403 before any handler runs.
    """
    daemon, port = _start(tmp_path)
    try:
        for path in ("/api/auth/status", "/api/readiness", "/"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path, headers={"Host": "evil.example:8643"})
            resp = conn.getresponse()
            assert resp.status == 403, (path, resp.read())
            conn.close()
    finally:
        daemon._stop_status_server()


def test_public_endpoints_allow_loopback_host(tmp_path):
    """Public endpoints remain reachable for legitimate loopback use."""
    daemon, port = _start(tmp_path)
    try:
        for path in ("/api/auth/status", "/api/readiness"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path, headers={"Host": f"127.0.0.1:{port}"})
            resp = conn.getresponse()
            assert resp.status == 200, (path, resp.read())
            conn.close()
    finally:
        daemon._stop_status_server()


def test_post_rejects_cross_origin_browser_request(tmp_path):
    """PRA-001 (Origin): a browser Origin not matching Host is blocked."""
    daemon, port = _start(tmp_path)
    try:
        payload = b'{"q":"x"}'
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/ask",
            body=payload,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": "http://evil.example",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        resp = conn.getresponse()
        assert resp.status == 403, resp.read()
        conn.close()
    finally:
        daemon._stop_status_server()


def test_post_allows_same_origin_browser_request(tmp_path):
    """A browser Origin matching the Host is accepted (then auth applies).

    Uses /api/model with an invalid model so the handler returns a quick 400
    rather than invoking the LLM — the point is to confirm the Origin gate
    passes (not 403) for a same-origin browser request.
    """
    daemon, port = _start(tmp_path)
    try:
        payload = json.dumps({"provider": "mock", "model": ""}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/model",
            body=payload,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        resp = conn.getresponse()
        body = resp.read()
        # Same-origin: the Origin gate must NOT block (status != 403).
        assert resp.status != 403, body
        conn.close()
    finally:
        daemon._stop_status_server()


def test_post_cli_without_origin_passes(tmp_path):
    """CLI clients (no Origin header) are unaffected by the Origin gate."""
    daemon, port = _start(tmp_path)
    try:
        payload = json.dumps({"provider": "mock", "model": ""}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/model",
            body=payload,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        resp = conn.getresponse()
        body = resp.read()
        assert resp.status != 403, body
        conn.close()
    finally:
        daemon._stop_status_server()


def test_login_fails_closed_when_no_token(tmp_path, monkeypatch):
    from hybridagent import auth_gate
    from hybridagent import config as cfg

    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    daemon, port = _start(tmp_path)
    try:
        assert auth_gate.configured_token() == ""
        payload = json.dumps({"token": "attacker-guess"}).encode()
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "POST",
            "/api/auth/login",
            body=payload,
            headers={
                "Host": f"127.0.0.1:{port}",
                "Content-Type": "application/json",
                "Content-Length": str(len(payload)),
            },
        )
        resp = conn.getresponse()
        body = json.loads(resp.read().decode())
        assert body.get("ok") is False
        conn.close()
    finally:
        daemon._stop_status_server()


def test_remote_client_get_approvals_requires_token(tmp_path, monkeypatch):
    from hybridagent import config as cfg
    from hybridagent import daemon as dmod

    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "unit-test-token")
    monkeypatch.setattr(dmod._StatusHandler, "_is_loopback", lambda self: False)
    # Bind 0.0.0.0 so a remote Host (10.0.0.32) passes the Host gate and the
    # token gate is actually exercised.
    daemon, port = _start(tmp_path, host="0.0.0.0")
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/approvals", headers={"Host": f"10.0.0.32:{port}"})
        resp = conn.getresponse()
        assert resp.status == 401, resp.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request(
            "GET",
            "/api/approvals",
            headers={
                "Host": f"10.0.0.32:{port}",
                "Authorization": "Bearer unit-test-token",
            },
        )
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        conn.close()

        # Public endpoints remain reachable for a remote peer with a Host that
        # matches the 0.0.0.0 bind (no DNS rebinding).
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/auth/status", headers={"Host": f"10.0.0.32:{port}"})
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("GET", "/api/readiness", headers={"Host": f"10.0.0.32:{port}"})
        resp = conn.getresponse()
        assert resp.status == 200, resp.read()
        conn.close()
    finally:
        daemon._stop_status_server()


def test_remote_client_rebinding_host_blocked_on_public(tmp_path, monkeypatch):
    """PRA-001: a remote peer with a foreign Host cannot reach public routes.

    Even when the daemon binds 0.0.0.0, a Host that is neither the bind host
    nor loopback is rejected at the universal Host gate before the handler.
    """
    from hybridagent import config as cfg
    from hybridagent import daemon as dmod

    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "unit-test-token")
    monkeypatch.setattr(dmod._StatusHandler, "_is_loopback", lambda self: False)
    daemon, port = _start(tmp_path, host="0.0.0.0")
    try:
        for path in ("/api/auth/status", "/api/readiness", "/api/approvals"):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", path, headers={"Host": "evil.example:8643"})
            resp = conn.getresponse()
            assert resp.status == 403, (path, resp.read())
            conn.close()
    finally:
        daemon._stop_status_server()
