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

import html
import http.client
import json
import os
import re
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


def _http_text(url: str, headers: dict, timeout: float = 15.0) -> str:
    """GET ``url`` and return the raw decoded body (for HTML endpoints)."""
    req = urllib.request.Request(url, headers=headers, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


# --------------------------------------------------------------- DuckDuckGo
# Keyless default backend: DuckDuckGo's HTML endpoint needs no API key, so
# research works out of the box. Parsed with ``re`` (stdlib only).
_DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
_DDG_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
_DDG_ANCHOR_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL)
_DDG_SNIPPET_RE = re.compile(
    r'class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL)


def _strip_html(fragment: str) -> str:
    """Drop tags and unescape entities from an HTML fragment -> plain text."""
    return html.unescape(re.sub(r"<[^>]+>", "", fragment)).strip()


def _decode_ddg_url(href: str) -> str:
    """Recover the real target from DuckDuckGo's ``/l/?uddg=<encoded>`` redirect."""
    if "uddg=" in href:
        try:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            target = (params.get("uddg") or [""])[0]
            if target:
                return target
        except ValueError:
            pass
    if href.startswith("//"):
        return "https:" + href
    return href


def _run_duckduckgo(query: str, max_results: int) -> list[SearchResult]:
    """Keyless web search via DuckDuckGo's HTML endpoint. Returns [] on failure."""
    url = (os.environ.get("PRAXIS_SEARCH_ENDPOINT") or _DDG_ENDPOINT) + "?" + \
        urllib.parse.urlencode({"q": query})
    try:
        body = _http_text(url, {"User-Agent": _DDG_UA})
        anchors = _DDG_ANCHOR_RE.findall(body)
        snippets = _DDG_SNIPPET_RE.findall(body)
        results: list[SearchResult] = []
        for i, (href, title_html) in enumerate(anchors[:max_results]):
            snippet = _strip_html(snippets[i]) if i < len(snippets) else ""
            results.append(SearchResult(
                _strip_html(title_html), _decode_ddg_url(href), snippet))
        return results
    except (OSError, http.client.HTTPException, ValueError) as exc:
        # Mirror _BACKENDS error handling: degrade to empty, never crash.
        _log.warning("duckduckgo search failed: %s", exc)
        return []


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

    Precedence:

    1. An explicit provider (``PRAXIS_SEARCH`` / ``agents.search.provider``) with
       its API key wins — returns a (possibly empty) list.
    2. Otherwise fall back to the keyless DuckDuckGo HTML endpoint so research
       works out of the box with zero configuration — returns a (possibly empty)
       list, *not* ``None``.
    3. Set ``PRAXIS_SEARCH_DISABLE_DEFAULT=1`` to disable the keyless default and
       restore the legacy ``None`` contract (honest-placeholder path) when no
       provider/key is configured.
    """
    query = (query or "").strip()
    n = max(1, min(int(max_results or 5), 10))
    prov = configured_provider()
    if prov is not None:
        key = os.environ.get(_KEY_ENV[prov]) or ""
        if key:
            if not query:
                return []
            try:
                return _BACKENDS[prov](query, key, n)
            except (OSError, http.client.HTTPException, ValueError, KeyError,
                    TypeError) as exc:
                # OSError covers URLError/TimeoutError/ConnectionReset; degrade.
                _log.warning("search provider %s failed: %s", prov, exc)
                return []
        # Provider named but no key: fall through to the keyless default below
        # unless the escape hatch is set.

    if os.environ.get("PRAXIS_SEARCH_DISABLE_DEFAULT") == "1":
        return None  # legacy contract -> honest placeholder

    # Keyless default: DuckDuckGo HTML endpoint (no API key required).
    if not query:
        return []
    return _run_duckduckgo(query, n)
