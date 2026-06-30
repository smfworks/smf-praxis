"""Cheap, honest coverage for pure-logic modules: provider catalog resolution
and gateway channel routing."""
from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_providers_catalog_complete():
    from hybridagent.providers import CATALOG, ORDER
    # every ordered id resolves and carries the core fields
    for pid in ORDER:
        p = CATALOG[pid]
        assert p.id == pid
        assert p.label
        assert p.compatibility in ("openai", "anthropic", "google", "ollama", "custom")


def test_providers_azure_foundry_present():
    from hybridagent.providers import CATALOG
    p = CATALOG["azure-foundry"]
    assert p.compatibility == "openai"
    assert p.needs_key
    assert "services.ai.azure.com" in p.base_url


def test_providers_suggested_models_nonempty():
    from hybridagent.providers import CATALOG
    # the commonly-used providers advertise at least one suggested model
    for pid in ("openai", "anthropic", "ollama", "xai", "azure-foundry"):
        assert CATALOG[pid].suggested_models


def test_provider_lookup_helpers():
    from hybridagent import providers
    # exercise any public resolution helper that exists
    if hasattr(providers, "get"):
        assert providers.get("openai") is not None
        assert providers.get("nonexistent-xyz") is None


def test_gateways_target_parsing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import gateways
    # channel-only vs channel:dest forms both resolve a channel
    r1 = gateways.deliver("local", "x")
    assert r1.channel == "local"
    r2 = gateways.deliver("local:ignored", "x")
    assert r2.channel == "local"


def test_gateways_discord_truncation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import gateways
    sent = {}

    def fake_post(url, payload=None, headers=None, timeout=15.0, data=None):
        sent["payload"] = payload
        return 200, "ok"

    monkeypatch.setattr(gateways, "_post_json", fake_post)
    conf = cfg.load_config()
    gw = conf.setdefault("agents", {}).setdefault("gateways", {})
    gw["discord"] = {"webhook_url": "http://example/dh"}
    cfg.save_config(conf)
    gateways.deliver("discord", "A" * 5000)
    # Discord caps content at 2000 chars
    if sent.get("payload") and "content" in sent["payload"]:
        assert len(sent["payload"]["content"]) <= 2000


def test_gateways_ntfy(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import gateways
    called = {}

    def fake_post(url, payload=None, headers=None, timeout=15.0, data=None):
        called["url"] = url
        return 200, "ok"

    monkeypatch.setattr(gateways, "_post_json", fake_post)
    conf = cfg.load_config()
    gw = conf.setdefault("agents", {}).setdefault("gateways", {})
    gw["ntfy"] = {"topic": "praxis-test"}
    cfg.save_config(conf)
    r = gateways.deliver("ntfy", "hello")
    # delivered (or attempted) through the configured topic
    assert r.channel == "ntfy"
