"""A2A client — call other agents over HTTP (Phase A / G4).

The live Praxis<->Praxis round trip is exercised against an in-process daemon;
pure routing/parsing is tested without network.
"""
from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_call_agent_tool_registered_as_send(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import RiskClass
    from hybridagent.tools import default_registry
    reg = default_registry()
    assert "call_agent" in reg.names()
    assert reg.get("call_agent").risk is RiskClass.SEND


def test_resolve_target_url_vs_peer(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.a2a_client import resolve_target
    peer = resolve_target("https://example.com")
    assert peer is not None and peer.base_url == "https://example.com"
    assert resolve_target("unknown-peer") is None
    assert resolve_target("") is None


def test_registered_peer_resolves(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    conf = cfg.load_config()
    conf.setdefault("agents", {}).setdefault("a2a", {}).setdefault("peers", {})[
        "buddy"] = {"url": "https://buddy.example/", "headers": {}}
    cfg.save_config(conf)
    from hybridagent.a2a_client import get_peer, list_peers
    assert "buddy" in list_peers()
    assert get_peer("buddy").base_url == "https://buddy.example/"


def test_call_agent_unknown_target_errors():
    from hybridagent.real_tools import call_agent
    assert "required" in call_agent(target="", goal="")
    out = call_agent(target="definitely-not-a-peer", goal="do x")
    assert "unknown agent peer" in out


def test_call_agent_is_held_for_approval(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.agent import PraxisAgent
    from hybridagent.broker import RiskClass
    a = PraxisAgent.persistent()
    dec = a.broker.authorize("agent", "call_agent", RiskClass.SEND,
                             {"target": "https://x", "goal": "y"}, cycle_id="t")
    assert dec.verdict.value == "needs_approval"


def test_live_praxis_to_praxis_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import time

    from hybridagent.a2a_client import A2AClient
    from hybridagent.daemon import Daemon
    d = Daemon.from_env()
    d._ensure_agent()
    d._start_status_server()
    d.running = True
    try:
        time.sleep(0.3)
        c = A2AClient(f"http://127.0.0.1:{d.status_port}")
        card = c.card()
        assert card.get("name")
        run = c.run("say hello")
        assert "status" in run or "summary" in run
    finally:
        d.running = False
        d._stop_status_server()
