"""Messaging gateways: channel routing, graceful failure, governed send_message."""
from hybridagent import config as cfg
from hybridagent import gateways


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_available_channels():
    chans = gateways.available_channels()
    for c in ("telegram", "slack", "discord", "webhook", "ntfy"):
        assert c in chans


def test_local_target_is_noop():
    r = gateways.deliver("local", "hi")
    assert r.ok and r.channel == "local"


def test_unknown_channel_fails_gracefully():
    r = gateways.deliver("nope", "hi")
    assert not r.ok
    assert "unknown channel" in r.detail


def test_telegram_missing_config_fails_gracefully(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    r = gateways.deliver("telegram", "hi")
    assert not r.ok
    assert "bot_token" in r.detail or "chat_id" in r.detail


def test_target_parsing_routes_destination(monkeypatch):
    captured = {}

    def fake_post(url, payload, headers=None, timeout=15.0):
        captured["url"] = url
        captured["payload"] = payload
        return 200, "ok"

    monkeypatch.setattr(gateways, "_post_json", fake_post)
    r = gateways.deliver("discord:https://discord.com/api/webhooks/abc", "hello")
    assert r.ok
    assert captured["url"] == "https://discord.com/api/webhooks/abc"
    assert captured["payload"]["content"] == "hello"


def test_env_substitution_in_config(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("TG_TOKEN", "secret-token")
    conf = cfg.load_config()
    conf.setdefault("agents", {}).setdefault("gateways", {})["telegram"] = {
        "bot_token": "${TG_TOKEN}", "chat_id": "999"}
    cfg.save_config(conf)

    captured = {}

    def fake_post(url, payload, headers=None, timeout=15.0):
        captured["url"] = url
        return 200, "ok"

    monkeypatch.setattr(gateways, "_post_json", fake_post)
    r = gateways.deliver("telegram", "hi")
    assert r.ok
    assert "secret-token" in captured["url"]   # ${TG_TOKEN} expanded


# ---------------------------------------------------------- governed send tool
def test_send_message_tool_registered_as_send_risk(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import RiskClass
    from hybridagent.tools import default_registry
    reg = default_registry()
    assert "send_message" in reg.names()
    assert reg.get("send_message").risk is RiskClass.SEND


def test_send_message_is_held_for_approval(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.agent import PraxisAgent
    from hybridagent.broker import RiskClass
    a = PraxisAgent.persistent()
    dec = a.broker.authorize(
        "agent", "send_message", RiskClass.SEND,
        {"target": "ntfy:x", "text": "hi"}, cycle_id="t")
    assert dec.verdict.value == "needs_approval"


def test_send_message_tool_validates_args():
    from hybridagent.real_tools import send_message
    assert "required" in send_message(target="", text="")
