"""D4 Memory Studio + global Command-Palette search."""
import json
import socket
import urllib.parse
import urllib.request

from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


# --------------------------------------------------------------------- store
def test_list_memory_all_and_by_tier(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_memory("durable", "fact one", "seed", "note")
    s.add_memory("episodic", "event two", "seed", "note")
    assert len(s.list_memory()) == 2
    dur = s.list_memory(tier="durable")
    assert len(dur) == 1 and dur[0]["text"] == "fact one"


# -------------------------------------------------------------- daemon memory
def test_daemon_memory_crud(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    add = d.memory_add("durable", "remember the budget cap", "dashboard")
    assert add["id"] and add["tier"] == "durable"

    ml = d.memory_list()
    assert ml["by_tier"].get("durable", 0) >= 1
    assert any(m["text"] == "remember the budget cap" for m in ml["items"])
    assert "working" in ml["tiers"]

    assert d.memory_add("durable", "  ").get("error")        # empty refused
    assert d.memory_delete(add["id"])["deleted"] is True


# -------------------------------------------------------------- daemon search
def test_daemon_search(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d.memory_add("durable", "the quarterly customer report", "dashboard")
    d.agent_run("follow up with the customer")

    res = d.search("customer")
    assert res["memory"] or res["runs"]      # finds at least one surface
    assert d.search("")["memory"] == []      # empty query -> nothing


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


def test_memory_and_search_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        made = _post(f"{url}/api/memory",
                     {"tier": "durable", "text": "alpha customer report"})
        assert made["id"]

        with urllib.request.urlopen(f"{url}/api/memory", timeout=10) as r:
            ml = json.loads(r.read())
        assert any(m["text"] == "alpha customer report" for m in ml["items"])

        qs = urllib.parse.urlencode({"q": "customer"})
        with urllib.request.urlopen(f"{url}/api/search?{qs}", timeout=10) as r:
            res = json.loads(r.read())
        assert any(m["id"] == made["id"] for m in res["memory"])

        assert _post(f"{url}/api/memory/delete", {"id": made["id"]})["deleted"] is True

        for asset in ("memory.js", "palette.js"):
            with urllib.request.urlopen(f"{url}/web/{asset}", timeout=10) as r:
                body = r.read().decode()
            assert "Praxis" in body
    finally:
        d._stop_status_server()
