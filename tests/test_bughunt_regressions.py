"""Regression tests for bugs found in the post-Phase-D bug hunt."""
import os

import pytest

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
    if os.name == "nt":
        pytest.skip("os.getuid()/--user uid mapping is POSIX-only")
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
        # The old bug did a full ~527k-iteration year scan before returning None;
        # up-front validation short-circuits. Generous ceiling tolerates slow/
        # shared CI runners (Windows) while still catching a regression to the
        # scan path, which grows well past this.
        assert elapsed < 0.25, f"{expr} took {elapsed:.3f}s (should fail fast)"


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


# BUG 7 (cron/persistence): rapid second tick double-fired a due job.
def test_cron_claim_prevents_double_fire(tmp_path, monkeypatch):
    import time
    _isolate(tmp_path, monkeypatch)
    from hybridagent.cron import CronScheduler
    from hybridagent.persistence import Store
    store = Store.open()
    sched = CronScheduler(store)
    job = sched.create("ping", "30m")
    jid = job["job_id"]
    store.set_cron_next_run(jid, time.time() - 10)
    first = sched.claim()
    second = sched.claim()   # before reschedule
    assert len(first) == 1
    assert len(second) == 0  # claimed job not returned twice


# BUG 6 (vault): PRAXIS_VAULT_KEY set without cryptography must warn, not silently
# store plaintext-equivalent base64 while pretending to encrypt.
def test_vault_warns_on_silent_encryption_downgrade(tmp_path, monkeypatch, caplog):
    import logging
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("PRAXIS_VAULT_KEY", "some-key")
    from hybridagent import vault
    # only meaningful when cryptography is absent (the degraded path)
    try:
        import cryptography  # noqa: F401
        return  # encryption available -> no downgrade to warn about
    except ImportError:
        pass
    with caplog.at_level(logging.WARNING):
        vault._fernet()
    assert any("NOT encrypted" in r.message or "cryptography" in r.message
               for r in caplog.records)


def test_vault_roundtrip_prefix_collision(tmp_path, monkeypatch):
    """A stored value that itself starts with 'f:' or 'b:' must round-trip."""
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("PRAXIS_VAULT_KEY", raising=False)
    import os

    from hybridagent.vault import CredentialVault
    v = CredentialVault()
    v.put("c", {"X": "f:not-really-fernet"})
    with v.inject("c"):
        assert os.environ.get("X") == "f:not-really-fernet"


# BUG 8 (a2a_client): an untrusted peer's response body was read unbounded.
def test_a2a_rejects_oversized_response():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from hybridagent import a2a_client as a2a

    class Big(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            self.rfile.read(ln)
            body = json.dumps({"summary": "A" * (a2a._MAX_RESPONSE_BYTES + 1000)}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except Exception:
                pass

    srv = HTTPServer(("127.0.0.1", 0), Big)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = a2a.call_agent(f"http://127.0.0.1:{port}/", "x")
        assert "error" in res
        assert "exceeds" in res["error"]
    finally:
        srv.shutdown()


def test_a2a_normal_response_works():
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from hybridagent import a2a_client as a2a

    class OK(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = json.dumps({"name": "praxis", "tools": []}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_POST(self):
            ln = int(self.headers.get("Content-Length", 0))
            self.rfile.read(ln)
            b = json.dumps({"summary": "done", "status": "ok"}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = HTTPServer(("127.0.0.1", 0), OK)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        res = a2a.call_agent(f"http://127.0.0.1:{port}/", "x")
        assert res.get("status") == "ok"
    finally:
        srv.shutdown()


# BUG 9 (sandbox.py): local string commands were hardcoded to "sh -c", which
# does not exist on Windows. Local execution must use the host shell.
def test_sandbox_local_string_command_no_sh_hardcode():
    import inspect

    from hybridagent import sandbox
    # run() must not pre-wrap with the POSIX-only ["sh", "-c", ...] for local
    src = inspect.getsource(sandbox.run)
    assert '"sh", "-c"' not in src.replace(" ", "").replace("'", '"') or "_as_posix" in src
    # the local runner uses shell=True for strings (host shell, cross-platform)
    local_src = inspect.getsource(sandbox._run_local)
    assert "shell=use_shell" in local_src


def test_sandbox_local_string_executes():
    from hybridagent.sandbox import run
    r = run("echo cross-platform", backend="local")
    assert r.ok and "cross-platform" in r.stdout


def test_sandbox_docker_still_posix_wrapped():
    from hybridagent.sandbox import _as_posix_argv
    # docker/ssh target Linux -> string wrapped with sh -c
    assert _as_posix_argv("echo x") == ["sh", "-c", "echo x"]
    assert _as_posix_argv(["echo", "x"]) == ["echo", "x"]


# BUG 10 (config/vault/identity): secret files claimed 0600 protection but on
# Windows chmod only flips the read-only bit. secure_file() must restrict
# cross-platform and the secret writers must call it.
def test_secure_file_restricts_on_posix(tmp_path):
    import os

    from hybridagent import config
    p = tmp_path / "secret.json"
    p.write_text("{}")
    assert config.secure_file(p) is True
    if os.name != "nt":
        assert (os.stat(p).st_mode & 0o777) == 0o600


def test_secret_writers_use_secure_file(tmp_path, monkeypatch):
    import os
    _isolate(tmp_path, monkeypatch)
    from hybridagent.identity import AgentIdentity, _identity_path
    from hybridagent.vault import CredentialVault
    CredentialVault().put("b", {"K": "v"})
    AgentIdentity.load_or_create("praxis")
    if os.name != "nt":
        from hybridagent.vault import _vault_path
        assert (os.stat(_vault_path()).st_mode & 0o777) == 0o600
        assert (os.stat(_identity_path()).st_mode & 0o777) == 0o600


# BUG 11 (daemon): a stalled/oversized Content-Length could wedge a handler
# thread forever. The status handler must set a socket timeout and clamp reads.
def test_status_handler_has_socket_timeout():
    from hybridagent.daemon import _StatusHandler
    # a bounded socket timeout frees a thread stalled on a lying Content-Length
    assert isinstance(_StatusHandler.timeout, (int, float))
    assert _StatusHandler.timeout and _StatusHandler.timeout <= 60


def test_status_handler_clamps_body_read():
    import inspect

    from hybridagent.daemon import _StatusHandler
    # the body reader clamps to a max_bytes ceiling rather than trusting the
    # declared Content-Length unbounded
    src = inspect.getsource(_StatusHandler._read_body)
    assert "max_bytes" in src and "min(" in src
