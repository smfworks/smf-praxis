"""D3 Observability: eval-trend, governance decision mix, and run-status metrics."""
import json
import socket
import urllib.request

from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


# --------------------------------------------------------------------- store
def test_audit_and_run_stats(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_audit("a", "t1", "read", "allow", "ok", policy_rule="autonomous_allow")
    s.add_audit("a", "t2", "send", "deny", "no", policy_rule="egress_blocked")
    st = s.audit_stats()
    assert st["total"] == 2
    assert st["by_verdict"]["allow"] == 1 and st["by_verdict"]["deny"] == 1
    assert st["by_rule"]["egress_blocked"] == 1

    s.start_run("r1", "g")
    s.finish_run("r1", "completed")
    rs = s.run_stats()
    assert rs["total"] == 1 and rs["by_status"]["completed"] == 1


# -------------------------------------------------------------------- daemon
def test_metrics_aggregates(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d.agent_run("follow up with the customer")  # produces audit + a run trace
    assert d.store is not None
    d.store.save_eval_run("{}", 30, 30)

    m = d.metrics()
    assert m["decisions"]["total"] >= 1
    assert "allow" in m["decisions"]["by_verdict"]
    assert m["runs"]["total"] >= 1
    assert m["evals"] and m["evals"][-1]["passes"] == 30


# ---------------------------------------------------------------- live HTTP
def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_metrics_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            f"{url}/api/agent/run",
            data=json.dumps({"goal": "follow up with the customer"}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            r.read()
        with urllib.request.urlopen(f"{url}/api/metrics", timeout=10) as r:
            m = json.loads(r.read())
        assert m["decisions"]["total"] >= 1 and m["runs"]["total"] >= 1
        with urllib.request.urlopen(f"{url}/web/metrics.js", timeout=10) as r:
            assert "Observability" in r.read().decode()
    finally:
        d._stop_status_server()
