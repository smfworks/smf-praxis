"""Preeminence sprint: persona, auth gate, pulse, growth, channels, TTFT."""
from __future__ import annotations

import json

import pytest

from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_persona_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.persona import load_persona, persona_system_prefix, save_persona
    p = save_persona({
        "display_name": "Alex",
        "role": "founder",
        "tone": "crisp",
        "never_do": "send without asking, post publicly",
    })
    assert p["display_name"] == "Alex"
    assert "send without asking" in p["never_do"]
    assert load_persona()["role"] == "founder"
    prefix = persona_system_prefix()
    assert "Alex" in prefix and "Never" in prefix


def test_auth_gate_loopback_open_and_token_match(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import auth_gate
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    assert auth_gate.auth_required("127.0.0.1") is False
    assert auth_gate.auth_required("0.0.0.0") is True
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "secret-token-xyz")
    assert auth_gate.token_matches("secret-token-xyz")
    assert not auth_gate.token_matches("wrong")
    assert not auth_gate.token_matches("")
    assert auth_gate.extract_token({"Authorization": "Bearer secret-token-xyz"}) \
        == "secret-token-xyz"


def test_auth_ensure_mints_token(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import auth_gate
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    tok = auth_gate.ensure_token()
    assert len(tok) >= 16
    assert auth_gate.configured_token() == tok
    # second call reuses
    assert auth_gate.ensure_token() == tok


def test_pulse_digest_shape(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    from hybridagent.llm import LLMClient
    d = Daemon(llm=LLMClient(mode="mock"), heartbeat_interval=9999)
    d._ensure_agent()
    dig = d.pulse_preview()
    assert "text" in dig and "Approvals waiting" in dig["text"]
    dig2 = d.pulse(target=None)
    assert dig2.get("delivered") is False


def test_growth_rooms_and_ttft(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.growth import list_rooms, record_ttft, ttft_stats
    rooms = list_rooms()
    assert any(r["id"] == "main" for r in rooms)
    record_ttft(12.5)
    record_ttft(8.0)
    st = ttft_stats()
    assert st["count"] == 2
    assert st["last"] == 8.0
    assert st["p50"] is not None


def test_growth_skills_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    from hybridagent.llm import LLMClient
    d = Daemon(llm=LLMClient(mode="mock"), heartbeat_interval=9999)
    d._ensure_agent()
    skills = d.growth_skills()
    assert isinstance(skills, list)


def test_channels_approval_deep_link():
    from hybridagent.channels_inbound import approval_deep_link, parse_telegram_update
    link = approval_deep_link("http://127.0.0.1:8643", "appr-abc")
    assert "approve=appr-abc" in link
    msg = parse_telegram_update({
        "message": {
            "text": "hello praxis",
            "chat": {"id": 99},
            "from": {"username": "alex"},
        }
    })
    assert msg is not None and msg.text == "hello praxis" and msg.chat_id == "99"


def test_daemon_persona_and_auth_endpoints(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import urllib.request

    from hybridagent.daemon import Daemon, _find_port
    from hybridagent.llm import LLMClient
    port = _find_port("127.0.0.1", 31000, 31100)
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port,
               heartbeat_interval=9999)
    d._start_status_server()
    try:
        base = f"http://127.0.0.1:{port}"
        with urllib.request.urlopen(f"{base}/api/auth/status") as resp:
            st = json.loads(resp.read().decode())
        assert "required" in st and st["required"] is False  # loopback
        req = urllib.request.Request(
            f"{base}/api/persona",
            data=json.dumps({"display_name": "Sam", "role": "ops"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
        assert body.get("ok") is True
        with urllib.request.urlopen(f"{base}/api/growth/model") as resp:
            model = json.loads(resp.read().decode())
        assert "Sam" in (model.get("summary") or model.get("persona", {}).get(
            "display_name") or "")
        with urllib.request.urlopen(f"{base}/api/growth/rooms") as resp:
            rooms = json.loads(resp.read().decode())
        assert rooms.get("rooms")
        with urllib.request.urlopen(f"{base}/api/browser/snapshot") as resp:
            snap = json.loads(resp.read().decode())
        assert "url" in snap or "error" in snap
    finally:
        d._stop_status_server()


def test_chat_system_includes_persona(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.daemon import Daemon
    from hybridagent.llm import LLMClient
    from hybridagent.persona import save_persona
    save_persona({"display_name": "Jordan", "role": "PM", "tone": "warm"})
    d = Daemon(llm=LLMClient(mode="mock"), heartbeat_interval=9999)
    sys = d._chat_system(None)
    assert "Jordan" in sys


def test_channel_threads_persist(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import channels_inbound as ch
    from hybridagent.persistence import Store
    store = Store.open()
    ch.set_thread_store(store)
    ch.append_thread("telegram", "42", "user", "hello", store=store)
    ch.append_thread("telegram", "42", "assistant", "hi there", store=store)
    # New process view: re-open store, rebind
    store2 = Store.open()
    hist = ch.get_thread("telegram", "42", store=store2)
    assert len(hist) == 2
    assert hist[0]["content"] == "hello"
    assert hist[1]["role"] == "assistant"


def test_evolution_proposals_persist_and_reject(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.persistence import Store
    store = Store.open()
    store.upsert_evolution_proposal(
        "evo-demo-1", "demo-skill",
        current_trigger="old", new_trigger="better trigger",
        current_body="body a", new_body="body b improved",
        current_fitness=0.1, new_fitness=0.5, improves=True,
        rationale="clearer", diff_text="---\n+++", source="test",
        status="pending",
    )
    rows = store.list_evolution_proposals(status="pending")
    assert len(rows) == 1
    assert rows[0]["skill_name"] == "demo-skill"
    assert rows[0]["id"] == "evo-demo-1"
    # Survives reopen
    store2 = Store.open()
    assert store2.get_evolution_proposal("evo-demo-1")["new_trigger"] == "better trigger"
    assert store2.resolve_evolution_proposal("evo-demo-1", "rejected")
    assert store2.list_evolution_proposals(status="pending") == []
    assert store2.get_evolution_proposal("evo-demo-1")["status"] == "rejected"


def test_growth_uses_store_for_proposals(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import growth
    from hybridagent.persistence import Store
    store = Store.open()
    growth.set_proposal_store(store)
    store.upsert_evolution_proposal(
        "evo-x-1", "x",
        current_trigger="t", new_trigger="t2",
        current_body="b", new_body="b2",
        current_fitness=0, new_fitness=1, improves=True,
        status="pending",
    )
    props = growth.list_proposals(store=store)
    assert any(p["id"] == "evo-x-1" for p in props)
    res = growth.reject_proposal("evo-x-1", store=store)
    assert res["rejected"] is True
    assert growth.list_proposals(store=store) == []


def test_telegram_configure_and_status(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import channels_inbound as ch
    st = ch.configure_telegram(
        bot_token="ENV", chat_id="12345", enabled=True, use_env_ref=True)
    assert st["enabled"] is True
    assert st["token_is_env_ref"] is True
    assert st["chat_id"] == "12345"
    conf = cfg.load_config()
    assert conf["agents"]["gateways"]["telegram"]["bot_token"] == "${TELEGRAM_BOT_TOKEN}"
    st2 = ch.disable_telegram()
    assert st2["enabled"] is False


def test_telegram_settings_api(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import urllib.request

    from hybridagent.daemon import Daemon, _find_port
    from hybridagent.llm import LLMClient
    port = _find_port("127.0.0.1", 31200, 31300)
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port,
               heartbeat_interval=9999)
    d._start_status_server()
    try:
        base = f"http://127.0.0.1:{port}"
        req = urllib.request.Request(
            f"{base}/api/channels/telegram",
            data=json.dumps({
                "action": "configure",
                "bot_token": "ENV",
                "chat_id": "99",
                "enabled": True,
                "use_env_ref": True,
            }).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
        assert body.get("enabled") is True
        assert body.get("chat_id") == "99"
        with urllib.request.urlopen(f"{base}/api/channels/telegram") as resp:
            st = json.loads(resp.read().decode())
        assert st.get("enabled") is True
        # Settings page still loads
        with urllib.request.urlopen(f"{base}/web/settings.js") as resp:
            js = resp.read().decode()
        assert "Enable Telegram" in js or "stTg" in js
    finally:
        d._stop_status_server()


def test_static_pwa_assets(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    import urllib.request

    from hybridagent.daemon import Daemon, _find_port
    from hybridagent.llm import LLMClient
    port = _find_port("127.0.0.1", 31100, 31200)
    d = Daemon(llm=LLMClient(mode="mock"), status_port=port,
               heartbeat_interval=9999)
    d._start_status_server()
    try:
        base = f"http://127.0.0.1:{port}"
        for path in ("/web/home.js", "/web/growth.js", "/web/manifest.webmanifest",
                     "/web/icon.svg", "/web/sw.js"):
            with urllib.request.urlopen(base + path) as resp:
                assert resp.status == 200
                data = resp.read()
                assert len(data) > 20
        with urllib.request.urlopen(base + "/") as resp:
            html = resp.read().decode()
        assert "home.js" in html and "growth.js" in html
        assert "manifest.webmanifest" in html
    finally:
        d._stop_status_server()
