"""D3 Inference Control: model/router info + an enforceable spend budget."""
import json
import socket
import urllib.request

from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


# --------------------------------------------------------------------- store
def test_budget_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    b = s.get_budget()                       # created on first use
    assert b["limit_usd"] == 0.0 and b["spent_usd"] == 0.0
    s.set_budget_limit(5.0)
    assert s.get_budget()["limit_usd"] == 5.0
    s.add_spend(1.25)
    s.add_spend(0.75)
    b = s.get_budget()
    assert abs(b["spent_usd"] - 2.0) < 1e-9 and b["runs"] == 2
    s.reset_budget()
    assert s.get_budget()["spent_usd"] == 0.0 and s.get_budget()["runs"] == 0


# -------------------------------------------------------------------- daemon
def test_inference_info_shape(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    info = d.inference_info()
    assert "model" in info and "router" in info and "budget" in info
    assert "researcher" in info["router"]["roles"]
    assert info["router"]["trained"] is False
    assert info["budget"]["over"] is False


def test_budget_cap_blocks_then_unblocks(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d.budget_set(0.001)                       # tiny cap

    first = d.agent_run("follow up with the customer")
    assert first["status"] != "blocked"       # first run executes...
    assert first["run_id"]
    assert d.budget_status()["over"] is True   # ...and pushes spend over the cap

    second = d.agent_run("research and summarize the latest sales numbers")
    assert second["status"] == "blocked" and second.get("blocked") is True
    assert second["run_id"] == ""              # never executed

    d.budget_set(100.0)                        # raising the cap unblocks
    third = d.agent_run("research and summarize the latest sales numbers")
    assert third["status"] != "blocked" and third["run_id"]

    d.budget_reset()
    assert d.budget_status()["spent_usd"] == 0.0


# ---------------------------------------------------------------- live HTTP
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


def test_inference_and_budget_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{url}/api/inference", timeout=10) as r:
            info = json.loads(r.read())
        assert info["router"]["roles"] and "budget" in info

        assert _post(f"{url}/api/budget", {"limit_usd": 12.5})["limit_usd"] == 12.5
        with urllib.request.urlopen(f"{url}/api/budget", timeout=10) as r:
            assert json.loads(r.read())["limit_usd"] == 12.5
        assert _post(f"{url}/api/budget", {"reset": True})["spent_usd"] == 0.0

        with urllib.request.urlopen(f"{url}/web/inference.js", timeout=10) as r:
            assert "Inference Control" in r.read().decode()
    finally:
        d._stop_status_server()
