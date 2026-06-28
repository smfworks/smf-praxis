"""D2 Safety Center: deny, kill-switch, and the audit-trail viewer API."""
import json
import socket
import urllib.request

from hybridagent import config as cfg
from hybridagent.llm import LLMClient


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _post(url, body):
    req = urllib.request.Request(
        url, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read())


# --------------------------------------------------------------- kill-switch
def test_killswitch_toggle(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    assert d.killswitch_status()["engaged"] is False
    assert d.killswitch_set(True)["engaged"] is True
    assert d.killswitch_status()["engaged"] is True
    assert d.killswitch_set(False)["engaged"] is False


def test_engaged_killswitch_denies_send(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d.killswitch_set(True)
    d.agent_run("follow up with the customer")
    # With the kill-switch engaged, the send step is denied, not held.
    audit = d.audit_log()["entries"]
    assert any(e["policy_rule"] == "kill_switch_denied" for e in audit)


# --------------------------------------------------------------- deny + audit
def test_deny_clears_pending_approval(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d.agent_run("follow up with the customer")  # holds a send for approval
    pend = d.list_approvals()
    assert pend, "expected a held send to approve/deny"
    d.deny_approval(pend[0]["approval_id"])
    assert all(p["approval_id"] != pend[0]["approval_id"] for p in d.list_approvals())


def test_audit_log_records_decisions(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d.agent_run("follow up with the customer")
    entries = d.audit_log()["entries"]
    assert entries and {"verdict", "tool", "policy_rule"} <= set(entries[0])


# ---------------------------------------------------------------- live HTTP
def test_safety_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        assert _post(f"{url}/api/killswitch", {"engaged": True})["engaged"] is True
        with urllib.request.urlopen(f"{url}/api/killswitch", timeout=10) as r:
            assert json.loads(r.read())["engaged"] is True
        _post(f"{url}/api/killswitch", {"engaged": False})

        _post(f"{url}/api/agent/run", {"goal": "follow up with the customer"})
        with urllib.request.urlopen(f"{url}/api/audit", timeout=10) as r:
            entries = json.loads(r.read())["entries"]
        assert entries and "verdict" in entries[0]

        with urllib.request.urlopen(f"{url}/web/safety.js", timeout=10) as r:
            assert "Safety Center" in r.read().decode()
    finally:
        d._stop_status_server()
