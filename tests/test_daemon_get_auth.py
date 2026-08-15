"""GET control-plane must honor Host integrity and remote token."""

from __future__ import annotations

import http.client
import json

from hybridagent.daemon import Daemon, _find_port
from hybridagent.llm import LLMClient


def _start(tmp_path):
    port = _find_port("127.0.0.1", 30000, 30100)
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port, work_dir=str(tmp_path))
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
    daemon, port = _start(tmp_path)
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
