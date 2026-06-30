"""Regression tests for bugs found in the post-Phase-D bug hunt."""
import os

from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


# BUG 1: docker sandbox hardcoded --user 1000:1000 broke non-uid-1000 hosts.
def test_docker_uses_host_uid_not_hardcoded():
    import inspect

    from hybridagent import sandbox
    src = inspect.getsource(sandbox._run_docker)
    # must not hardcode 1000:1000 as the literal user; must derive from getuid
    assert '"1000:1000"' not in src.replace(" ", "") or "getuid" in src
    assert "getuid" in src


def test_docker_user_matches_current_uid(monkeypatch):
    """The --user value handed to docker is the host's real uid:gid."""
    from hybridagent import sandbox
    captured = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox._run_docker(["echo", "x"], ".", 10, None, image="img",
                        network="none", mem="512m", pids=128)
    cmd = captured["cmd"]
    uidgid = cmd[cmd.index("--user") + 1]
    assert uidgid == f"{os.getuid()}:{os.getgid()}"


# BUG 2: out-of-range cron fields triggered a ~527k-iteration scan before None.
def test_invalid_cron_fields_fail_fast():
    import time

    from hybridagent.cron import compute_next_run
    for expr in ["60 * * * *", "0 24 * * *", "0 0 32 * *", "0 0 * 13 *",
                 "0 0 * * 9", "*/0 * * * *"]:
        t0 = time.time()
        nr = compute_next_run(expr)
        elapsed = time.time() - t0
        assert nr.ts is None, f"{expr} should be unparseable"
        assert elapsed < 0.05, f"{expr} took {elapsed:.3f}s (should fail fast)"


def test_valid_cron_still_works():
    from datetime import datetime

    from hybridagent.cron import compute_next_run
    nr = compute_next_run("0 9 * * 1")  # Mondays 09:00
    assert nr.ts is not None
    assert datetime.fromtimestamp(nr.ts).hour == 9
    # range boundaries are valid (each field at its max, no dom+dow conflict)
    assert compute_next_run("59 23 28 12 *").ts is not None
    assert compute_next_run("0 0 1 1 *").ts is not None
    assert compute_next_run("0 0 * * 6").ts is not None  # Saturdays


# BUG 3: marketplace silently accepted a version downgrade.
def test_marketplace_rejects_version_downgrade(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    src = tmp_path / "p.py"
    src.write_text("def register(r):\n    pass\n")
    from hybridagent import marketplace as mk
    assert mk.publish(str(src), name="p", version="1.2.0").get("published") == "p"
    res = mk.publish(str(src), name="p", version="1.1.0")
    assert "error" in res and "older" in res["error"]
    # same or newer is fine
    assert mk.publish(str(src), name="p", version="1.2.0").get("published") == "p"
    assert mk.publish(str(src), name="p", version="2.0.0").get("published") == "p"


def test_version_tuple_parsing():
    from hybridagent.marketplace import _version_tuple
    assert _version_tuple("1.2.3") == (1, 2, 3)
    assert _version_tuple("0.1.0") < _version_tuple("0.2.0")
    assert _version_tuple("1.0") < _version_tuple("1.0.1")
    assert _version_tuple("v2.0") == (2, 0)  # tolerant of 'v' prefix


# BUG 5 (broker): policy_hook "allow" must NOT bypass the egress firewall.
def test_policy_hook_allow_cannot_bypass_egress(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass

    def allow_all(ctx):
        return "allow"

    b = GovernanceBroker(policy=GovernancePolicy(
        allowed_tools={"send_message"}, policy_hook=allow_all))
    b.mark_tainted("injection-flagged-secret")
    dec = b.authorize("agent", "send_message", RiskClass.SEND,
                      {"text": "injection-flagged-secret exfiltrated"})
    # the egress firewall must still win over a convenience "allow"
    assert dec.verdict.value == "deny"
    assert "egress" in dec.reason


def test_policy_hook_allow_cannot_bypass_allowlist(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass

    def allow_all(ctx):
        return "allow"

    b = GovernanceBroker(policy=GovernancePolicy(
        allowed_tools={"read_file"}, policy_hook=allow_all))
    # delete_file not in allowlist -> deny despite the hook's allow
    dec = b.authorize("agent", "delete_file", RiskClass.DESTRUCTIVE,
                      {"path": "/etc/x"})
    assert dec.verdict.value == "deny"


def test_policy_hook_allow_still_waives_approval_when_safe(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass

    def allow_all(ctx):
        return "allow"

    b = GovernanceBroker(policy=GovernancePolicy(
        allowed_tools={"send_message"}, policy_hook=allow_all))
    # no taint, in allowlist, kill-switch clear -> allow waives human approval
    dec = b.authorize("agent", "send_message", RiskClass.SEND,
                      {"text": "totally benign status update"})
    assert dec.verdict.value == "allow"


# BUG 4 (mcp_client): HTTP-error responses with a JSON-RPC body must surface the
# server's message, not a bare "HTTP Error 400".
def test_http_transport_surfaces_jsonrpc_error_body():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from hybridagent.mcp_client import HttpTransport, MCPError

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(ln) or "{}")
            body = json.dumps({"jsonrpc": "2.0", "id": req.get("id"),
                               "error": {"code": -32000,
                                         "message": "server says no"}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t = HttpTransport(f"http://127.0.0.1:{port}/mcp")
        try:
            t.request("tools/list", timeout=5)
            assert False, "should have raised MCPError"
        except MCPError as exc:
            assert "server says no" in str(exc)
    finally:
        srv.shutdown()


def test_http_transport_rejects_id_mismatch():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from hybridagent.mcp_client import HttpTransport, MCPError

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            self.rfile.read(ln)
            body = json.dumps({"jsonrpc": "2.0", "id": 99999,
                               "result": {"ok": True}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        t = HttpTransport(f"http://127.0.0.1:{port}/mcp")
        try:
            t.request("tools/list", timeout=5)
            assert False, "should reject mismatched response id"
        except MCPError as exc:
            assert "id" in str(exc).lower()
    finally:
        srv.shutdown()
