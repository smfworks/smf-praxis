"""Web search backends (stdlib-only).

Turns ``search_web`` from a stub into real results when a provider is configured,
gated by an API key, with an honest offline placeholder otherwise. Mirrors
``providers.py``: pick a backend, resolve its key, drive ``urllib``, parse JSON —
no third-party dependencies.

Configure with ``PRAXIS_SEARCH=tavily|brave|serpapi`` (or ``agents.search.provider``
in ``praxis.json``) plus the provider's key env var (``TAVILY_API_KEY`` /
``BRAVE_API_KEY`` / ``SERPAPI_API_KEY``). ``PRAXIS_SEARCH_ENDPOINT`` overrides the
endpoint for a self-hosted proxy or tests.
"""
from __future__ import annotations

import http.client
import json
import os
import urllib.parse
import urllib.request
from dataclasses import dataclass

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.search")

_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "brave": "https://api.search.brave.com/res/v1/web/search",
    "serpapi": "https://serpapi.com/search.json",
}
_KEY_ENV = {
    "tavily": "TAVILY_API_KEY",
    "brave": "BRAVE_API_KEY",
    "serpapi": "SERPAPI_API_KEY",
}


@dataclass
class SearchResult:
    title: str
    url: str
    snippet: str


def _endpoint(provider: str) -> str:
    return os.environ.get("PRAXIS_SEARCH_ENDPOINT") or _ENDPOINTS[provider]


def _http(url: str, headers: dict, data: bytes | None = None,
          timeout: float = 15.0) -> dict:
    req = urllib.request.Request(
        url, data=data, headers=headers, method="POST" if data else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _parse_tavily(d: dict, n: int) -> list[SearchResult]:
    return [SearchResult(str(r.get("title", "")), str(r.get("url", "")),
                         str(r.get("content", "")))
            for r in (d.get("results") or [])[:n]]


def _parse_brave(d: dict, n: int) -> list[SearchResult]:
    items = ((d.get("web") or {}).get("results")) or []
    return [SearchResult(str(r.get("title", "")), str(r.get("url", "")),
                         str(r.get("description", "")))
            for r in items[:n]]


def _parse_serpapi(d: dict, n: int) -> list[SearchResult]:
    return [SearchResult(str(r.get("title", "")), str(r.get("link", "")),
                         str(r.get("snippet", "")))
            for r in (d.get("organic_results") or [])[:n]]


def _run_tavily(query: str, key: str, n: int) -> list[SearchResult]:
    data = json.dumps({"api_key": key, "query": query, "max_results": n}).encode()
    return _parse_tavily(
        _http(_endpoint("tavily"), {"Content-Type": "application/json"}, data), n)


def _run_brave(query: str, key: str, n: int) -> list[SearchResult]:
    url = _endpoint("brave") + "?" + urllib.parse.urlencode({"q": query, "count": n})
    return _parse_brave(
        _http(url, {"Accept": "application/json", "X-Subscription-Token": key}), n)


def _run_serpapi(query: str, key: str, n: int) -> list[SearchResult]:
    url = _endpoint("serpapi") + "?" + urllib.parse.urlencode(
        {"q": query, "api_key": key, "engine": "google", "num": n})
    return _parse_serpapi(_http(url, {"Accept": "application/json"}), n)


_BACKENDS = {"tavily": _run_tavily, "brave": _run_brave, "serpapi": _run_serpapi}


def configured_provider() -> str | None:
    """The configured search provider (env first, then agents.search.provider)."""
    prov = os.environ.get("PRAXIS_SEARCH")
    if not prov:
        try:
            prov = (cfg.load_config().get("agents", {}).get("search", {})
                    or {}).get("provider")
        except Exception:  # config unreadable (e.g. no home dir) -> env-only
            prov = None
    prov = prov.lower().strip() if isinstance(prov, str) else None
    return prov if prov in _BACKENDS else None


def web_search(query: str, max_results: int = 5) -> list[SearchResult] | None:
    """Real web search via the configured provider.

    Returns a (possibly empty) result list when a provider *and* key are
    configured, or ``None`` when nothing is configured — letting the caller fall
    back to an honest placeholder instead of pretending to search.
    """
    query = (query or "").strip()
    prov = configured_provider()
    if prov is None:
        return None
    key = os.environ.get(_KEY_ENV[prov]) or ""
    if not key:
        return None
    if not query:
        return []
    n = max(1, min(int(max_results or 5), 10))
    try:
        return _BACKENDS[prov](query, key, n)
    except (OSError, http.client.HTTPException, ValueError, KeyError,
            TypeError) as exc:
        # OSError covers URLError/TimeoutError/ConnectionReset; degrade, don't crash.
        _log.warning("search provider %s failed: %s", prov, exc)
        return []
