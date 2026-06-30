"""Microsoft Foundry provider + HTTP MCP transport / xAI Docs preset.

Network-touching MCP tests are mocked so the suite stays hermetic and offline;
a single opt-in live test is guarded behind PRAXIS_LIVE_MCP=1.
"""
import os

import pytest

from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


# --------------------------------------------------------------- Foundry provider
def test_azure_foundry_provider_registered():
    from hybridagent.providers import CATALOG, ORDER
    assert "azure-foundry" in CATALOG
    assert "azure-foundry" in ORDER
    p = CATALOG["azure-foundry"]
    assert p.compatibility == "openai"
    assert p.needs_key is True
    assert p.key_env == "AZURE_AI_API_KEY"
    assert "services.ai.azure.com" in p.base_url


def test_azure_foundry_pricing_resolves_for_hosted_models():
    # Foundry hosts many vendors; pricing should resolve by model substring.
    from hybridagent.pricing import price_usd
    # A cloud model via Foundry is billed (non-zero) using the substring match.
    cost = price_usd("azure-foundry/gpt-4o", 1000, 1000)
    assert cost > 0


# ------------------------------------------------------------- HTTP MCP transport
class _FakeResp:
    def __init__(self, body, ctype="application/json", session=None):
        self._body = body.encode()
        self.headers = {"Content-Type": ctype}
        if session:
            self.headers["Mcp-Session-Id"] = session

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_http_transport_parses_json(monkeypatch):
    import json

    from hybridagent import mcp_client

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        payload = json.loads(req.data.decode())
        mid = payload.get("id")
        return _FakeResp(json.dumps(
            {"jsonrpc": "2.0", "id": mid,
             "result": {"serverInfo": {"name": "fake"}}}))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    t = mcp_client.HttpTransport("https://example.com/mcp")
    out = t.request("initialize", {})
    assert out["serverInfo"]["name"] == "fake"


def test_http_transport_parses_sse(monkeypatch):
    import json

    from hybridagent import mcp_client

    def fake_urlopen(req, timeout=None):
        payload = json.loads(req.data.decode())
        mid = payload.get("id")
        sse = (f"event: message\ndata: "
               f"{json.dumps({'jsonrpc':'2.0','id':mid,'result':{'ok':True}})}\n\n")
        return _FakeResp(sse, ctype="text/event-stream")

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    t = mcp_client.HttpTransport("https://example.com/mcp")
    out = t.request("tools/list", {})
    assert out["ok"] is True


def test_expand_env_substitutes_secrets(monkeypatch):
    from hybridagent.mcp_client import _expand_env
    monkeypatch.setenv("MY_SECRET_KEY", "sk-123")
    headers = _expand_env({"Authorization": "Bearer ${MY_SECRET_KEY}"})
    assert headers["Authorization"] == "Bearer sk-123"


# ------------------------------------------------------------------ xAI preset
def test_xai_docs_preset_exists():
    from hybridagent import mcp_presets
    assert "xai-docs" in mcp_presets.preset_names()
    p = mcp_presets.get_preset("xai-docs")
    assert p["url"] == "https://docs.x.ai/api/mcp"
    assert p["risk"]["search_docs"] == "read"


def test_enable_preset_writes_config(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import mcp_presets
    res = mcp_presets.enable_preset("xai-docs")
    assert res["enabled"] == "xai-docs"
    servers = (cfg.load_config()["agents"]["mcp"]["servers"])
    assert "xai-docs" in servers
    assert servers["xai-docs"]["url"] == "https://docs.x.ai/api/mcp"
    # Idempotent re-enable doesn't duplicate or error.
    assert mcp_presets.enable_preset("xai-docs")["enabled"] == "xai-docs"


def test_enable_unknown_preset_errors(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import mcp_presets
    assert "error" in mcp_presets.enable_preset("nope-not-real")


@pytest.mark.skipif(os.environ.get("PRAXIS_LIVE_MCP") != "1",
                    reason="set PRAXIS_LIVE_MCP=1 to hit the live xAI Docs MCP")
def test_live_xai_docs_mcp_roundtrip():
    from hybridagent.mcp_client import MCPClient, mcp_tools
    c = MCPClient.connect_http("https://docs.x.ai/api/mcp")
    try:
        c.initialize()
        tools = mcp_tools(c, server_name="xai_docs")
        names = {t.name for t in tools}
        assert any("search_docs" in n for n in names)
        assert all(t.risk.value == "read" for t in tools)
    finally:
        c.close()
