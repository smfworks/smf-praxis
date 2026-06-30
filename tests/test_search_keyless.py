"""Keyless DuckDuckGo HTML default backend (zero-config web search)."""
import urllib.parse

from hybridagent import config as cfg
from hybridagent import search
from hybridagent.search import (
    SearchResult,
    _decode_ddg_url,
    _run_duckduckgo,
    web_search,
)


def _ddg_redirect(real_url: str) -> str:
    """Build a DuckDuckGo ``/l/?uddg=<encoded>`` redirect like the real site."""
    return "//duckduckgo.com/l/?uddg=" + urllib.parse.quote(real_url, safe="")


def _canned_html() -> str:
    """Minimal DuckDuckGo HTML page with two results + redirect-wrapped URLs."""
    href1 = _ddg_redirect("https://example.com/praxis")
    href2 = _ddg_redirect("https://example.org/agents")
    return f"""
    <html><body>
      <div class="result">
        <a class="result__a" href="{href1}">Praxis &amp; Governance</a>
        <a class="result__snippet" href="{href1}">A <b>governed</b> agent runtime.</a>
      </div>
      <div class="result">
        <a class="result__a" href="{href2}">Agents 101</a>
        <a class="result__snippet" href="{href2}">Intro to autonomous agents.</a>
      </div>
    </body></html>
    """


# --------------------------------------------------------------- url decoding
def test_decode_ddg_redirect_recovers_real_url():
    real = "https://example.com/a?b=c&d=e"
    assert _decode_ddg_url(_ddg_redirect(real)) == real
    # Plain (non-redirect) URLs pass through; protocol-relative gets https.
    assert _decode_ddg_url("https://plain.example/x") == "https://plain.example/x"
    assert _decode_ddg_url("//cdn.example/y") == "https://cdn.example/y"


# ------------------------------------------------------------------- parsing
def test_run_duckduckgo_parses_titles_urls_snippets(monkeypatch):
    monkeypatch.setattr(search, "_http_text", lambda *a, **k: _canned_html())
    results = _run_duckduckgo("praxis", 5)
    assert len(results) == 2
    assert results[0] == SearchResult(
        "Praxis & Governance", "https://example.com/praxis",
        "A governed agent runtime.")
    assert results[1].title == "Agents 101"
    assert results[1].url == "https://example.org/agents"
    assert results[1].snippet == "Intro to autonomous agents."


def test_run_duckduckgo_respects_max_results(monkeypatch):
    monkeypatch.setattr(search, "_http_text", lambda *a, **k: _canned_html())
    assert len(_run_duckduckgo("praxis", 1)) == 1


def test_run_duckduckgo_degrades_to_empty_on_network_error(monkeypatch):
    def _boom(*_a, **_k):
        raise OSError("no network")
    monkeypatch.setattr(search, "_http_text", _boom)
    assert _run_duckduckgo("praxis", 5) == []


# ------------------------------------------------------- web_search wiring
def test_web_search_falls_back_to_duckduckgo_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    for v in ("PRAXIS_SEARCH", "PRAXIS_SEARCH_DISABLE_DEFAULT",
              "PRAXIS_SEARCH_ENDPOINT"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr(search, "_http_text", lambda *a, **k: _canned_html())
    results = web_search("praxis", max_results=5)
    assert results is not None                      # keyless default -> not None
    assert len(results) == 2
    assert results[0].url == "https://example.com/praxis"


def test_web_search_disable_default_restores_none(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_SEARCH", raising=False)
    monkeypatch.setenv("PRAXIS_SEARCH_DISABLE_DEFAULT", "1")
    # Should never touch the network when the default is disabled.
    monkeypatch.setattr(search, "_http_text", lambda *a, **k: _canned_html())
    assert web_search("praxis") is None


def test_explicit_provider_with_key_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_SEARCH", "tavily")
    monkeypatch.setenv("TAVILY_API_KEY", "secret-key")
    monkeypatch.delenv("PRAXIS_SEARCH_DISABLE_DEFAULT", raising=False)

    sentinel = [SearchResult("Tavily Hit", "https://tavily.example/x", "via tavily")]

    def _fake_tavily(query, key, n):
        assert key == "secret-key"
        return sentinel

    monkeypatch.setitem(search._BACKENDS, "tavily", _fake_tavily)
    # DuckDuckGo must NOT be consulted when an explicit provider+key win.
    def _ddg_should_not_run(*_a, **_k):
        raise AssertionError("DuckDuckGo should not be used when tavily is set")
    monkeypatch.setattr(search, "_http_text", _ddg_should_not_run)

    results = web_search("praxis")
    assert results == sentinel


def test_provider_without_key_falls_back_to_duckduckgo(tmp_path, monkeypatch):
    # Provider named but no key available -> keyless default still kicks in.
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_SEARCH", "tavily")
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("PRAXIS_SEARCH_DISABLE_DEFAULT", raising=False)
    monkeypatch.setattr(search, "_http_text", lambda *a, **k: _canned_html())
    results = web_search("praxis")
    assert results is not None and len(results) == 2
