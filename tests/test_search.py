"""P2 web search: provider-backed real search with an honest offline fallback."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from hybridagent import config as cfg
from hybridagent.real_tools import search_web
from hybridagent.search import (
    SearchResult,
    _parse_brave,
    _parse_serpapi,
    _parse_tavily,
    web_search,
)


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# --------------------------------------------------------------------- parsers
def test_parsers_normalise_each_provider_shape():
    tav = _parse_tavily({"results": [{"title": "T", "url": "u", "content": "c"}]}, 5)
    assert tav == [SearchResult("T", "u", "c")]
    brave = _parse_brave(
        {"web": {"results": [{"title": "B", "url": "u2", "description": "d"}]}}, 5)
    assert brave == [SearchResult("B", "u2", "d")]
    serp = _parse_serpapi(
        {"organic_results": [{"title": "S", "link": "u3", "snippet": "s"}]}, 5)
    assert serp == [SearchResult("S", "u3", "s")]
    assert _parse_brave({}, 5) == []           # missing keys -> empty, no crash


# ----------------------------------------------------------------- unconfigured
def test_unconfigured_returns_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    for v in ("PRAXIS_SEARCH", "PRAXIS_SEARCH_URL", "PRAXIS_SEARCH_ENDPOINT"):
        monkeypatch.delenv(v, raising=False)
    # Disable the keyless DuckDuckGo default to exercise the legacy None contract.
    monkeypatch.setenv("PRAXIS_SEARCH_DISABLE_DEFAULT", "1")
    assert web_search("anything") is None
    out = search_web("anything")
    assert "no search backend configured" in out


def test_configured_without_key_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_SEARCH", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    # Disable the keyless default so the missing-key path returns None as before.
    monkeypatch.setenv("PRAXIS_SEARCH_DISABLE_DEFAULT", "1")
    assert web_search("q") is None              # gated: no key -> placeholder path


def test_provider_error_degrades_to_empty(tmp_path, monkeypatch):
    # A network failure (nothing listening) must degrade to [], never crash —
    # OSError/TimeoutError are not URLError subclasses.
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_SEARCH", "brave")
    monkeypatch.setenv("BRAVE_API_KEY", "k")
    monkeypatch.setenv("PRAXIS_SEARCH_ENDPOINT", f"http://127.0.0.1:{_free_port()}/x")
    assert web_search("q") == []


# ------------------------------------------------------------------ end to end
class _BraveStub(BaseHTTPRequestHandler):
    def log_message(self, *_a):
        pass

    def do_GET(self):
        body = json.dumps({"web": {"results": [
            {"title": "Praxis", "url": "https://example.com/p",
             "description": "a governed agent"},
            {"title": "Two", "url": "https://example.com/2", "description": "second"},
        ]}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_brave_end_to_end_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _BraveStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        monkeypatch.setenv("PRAXIS_SEARCH", "brave")
        monkeypatch.setenv("BRAVE_API_KEY", "test-key")
        monkeypatch.setenv("PRAXIS_SEARCH_ENDPOINT", f"http://127.0.0.1:{port}/search")

        results = web_search("governed agent", max_results=2)
        assert results and results[0].title == "Praxis"
        assert results[0].url == "https://example.com/p"

        out = search_web("governed agent")
        assert "Praxis" in out and "example.com/p" in out and "result(s)" in out
    finally:
        srv.shutdown()
