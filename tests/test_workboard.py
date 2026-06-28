"""D2 Work Board: a governed kanban whose Run executes the card under the broker."""
import json
import socket
import urllib.request

from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


# --------------------------------------------------------------------- store
def test_board_card_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.db")
    s.add_card("c1", "Title", "do the thing")
    cards = s.list_cards()
    assert cards[0]["card_id"] == "c1" and cards[0]["lane"] == "backlog"

    s.move_card("c1", "planned")
    assert s.get_card("c1")["lane"] == "planned"

    s.set_card_run("c1", "run-x", "completed", "done")
    c = s.get_card("c1")
    assert c["run_id"] == "run-x" and c["status"] == "completed" and c["lane"] == "done"

    s.delete_card("c1")
    assert s.get_card("c1") is None


# -------------------------------------------------------------------- daemon
def test_daemon_board_run_reflects_governed_status(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))

    created = d.board_create("Follow up", "follow up with the customer")
    cid = created["card"]["card_id"]
    assert created["card"]["lane"] == "backlog"

    out = d.board_run(cid)
    card = out["card"]
    assert card["run_id"]                      # linked to a durable run trace
    assert card["lane"] in ("done", "held", "failed")
    assert card["status"] == out["result"]["status"]

    assert d.board_move(cid, "backlog")["card"]["lane"] == "backlog"
    assert d.board_move(cid, "bogus").get("error")   # invalid lane refused
    assert d.board_move("nope", "done").get("error")  # unknown card refused

    d.board_delete(cid)
    assert all(c["card_id"] != cid for c in d.board_list()["cards"])


def test_board_create_requires_goal(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    assert d.board_create("", "").get("error")


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


def test_board_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    port = _free_port()
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port)
    d._start_status_server()
    try:
        url = f"http://127.0.0.1:{port}"
        card = _post(f"{url}/api/board/create",
                     {"title": "demo", "goal": "follow up with the customer"})["card"]
        cid = card["card_id"]

        ran = _post(f"{url}/api/board/run", {"card_id": cid})
        assert ran["card"]["run_id"] and ran["card"]["lane"] in ("done", "held", "failed")

        with urllib.request.urlopen(f"{url}/api/board", timeout=10) as r:
            board = json.loads(r.read())
        assert any(c["card_id"] == cid for c in board["cards"])
        assert board["lanes"][0] == "backlog"

        moved = _post(f"{url}/api/board/move", {"card_id": cid, "lane": "planned"})
        assert moved["card"]["lane"] == "planned"

        assert _post(f"{url}/api/board/delete", {"card_id": cid})["deleted"] == cid

        with urllib.request.urlopen(f"{url}/web/board.js", timeout=10) as r:
            assert "PraxisRunGraph" in r.read().decode()
    finally:
        d._stop_status_server()
