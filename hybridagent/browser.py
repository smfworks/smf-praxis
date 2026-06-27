"""Governed computer/browser-use tools.

Stateful browser automation exposed as risk-classified tools that flow through
the same GovernanceBroker as everything else: **navigating and reading** a page
are ``READ`` (autonomous), while **clicking and typing** are consequential
(``SEND``) — held for human approval — because they can submit forms or trigger
real actions.

The backend is **Playwright** (the optional ``[browser]`` extra) when installed;
otherwise navigation/reading fall back to a dependency-free stdlib fetch +
HTML-to-text extraction, and interaction reports that it needs the extra. The
governance is identical either way.
"""
from __future__ import annotations

import re
import threading
from html.parser import HTMLParser
from typing import Any

from .broker import RiskClass
from .tools import Tool


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag in ("script", "style", "noscript", "template"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "noscript", "template") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        s = data.strip()
        if s:
            self.chunks.append(s)


def _html_to_text(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html or "")
    except Exception:  # malformed HTML -> crude tag strip
        return " ".join(re.sub(r"<[^>]+>", " ", html or "").split())
    return " ".join(parser.chunks)


def _extract_title(html: str) -> str:
    match = re.search(r"<title[^>]*>(.*?)</title>", html or "",
                      re.IGNORECASE | re.DOTALL)
    return (match.group(1).strip() if match else "")[:200]


def _http_get(url: str) -> str:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(
        url, headers={"User-Agent": "PraxisAgent/0.13 (+browser)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.read(2_000_000).decode("utf-8", errors="replace")
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        return f"<title>error</title>error fetching {url}: {exc}"


class BrowserSession:
    """A stateful browsing session. Uses Playwright when available, else a
    dependency-free read-only fetch for navigation/reading."""

    def __init__(self, allow_playwright: bool = True) -> None:
        self._page: Any = None
        self._pw: Any = None
        self._browser: Any = None
        self._lock = threading.Lock()
        self.allow_playwright = allow_playwright
        self.url = ""
        self.title = ""
        self.text = ""

    def _playwright(self) -> bool:
        if self._page is not None:
            return True
        if not self.allow_playwright:
            return False
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
            return True
        except Exception:  # package missing or no browser binaries -> fallback
            self._page = self._browser = self._pw = None
            return False

    def navigate(self, url: str) -> str:
        from urllib.parse import urlparse
        if urlparse(url).scheme not in ("http", "https"):
            return f"[browser] unsupported URL scheme: {url!r}"
        with self._lock:
            if self._playwright():
                self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                self.url = self._page.url
                self.title = self._page.title()
                self.text = self._page.inner_text("body")[:20000]
            else:
                html = _http_get(url)
                self.url = url
                self.title = _extract_title(html)
                self.text = _html_to_text(html)[:20000]
        return f"[browser] {self.title or '(no title)'} — {self.url}\n{self.text[:1500]}"

    def read(self) -> str:
        if not self.text:
            return "[browser] no page loaded; navigate first"
        return self.text[:4000]

    def find(self, query: str) -> str:
        if not self.text:
            return "[browser] no page loaded; navigate first"
        needle = query.lower()
        hits = [seg.strip() for seg in re.split(r"(?<=[.!?])\s+", self.text)
                if needle in seg.lower()]
        return ("\n".join(hits[:10]) if hits
                else f"[browser] '{query}' not found on the current page")

    def click(self, target: str) -> str:
        with self._lock:
            if not self._playwright():
                return ("[browser] interaction requires the optional [browser] "
                        "extra (pip install praxis-agent[browser])")
            self._page.click(target, timeout=10000)
            self.text = self._page.inner_text("body")[:20000]
            self.url = self._page.url
        return f"[browser] clicked {target!r}; now at {self.url}"

    def type_text(self, target: str, text: str) -> str:
        with self._lock:
            if not self._playwright():
                return ("[browser] interaction requires the optional [browser] "
                        "extra (pip install praxis-agent[browser])")
            self._page.fill(target, text)
        return f"[browser] typed {len(text)} chars into {target!r}"

    def close(self) -> None:
        with self._lock:
            try:
                if self._browser is not None:
                    self._browser.close()
                if self._pw is not None:
                    self._pw.stop()
            except Exception:
                pass
            self._page = self._browser = self._pw = None


_SESSION = BrowserSession()

_URL_SCHEMA = {"type": "object", "properties": {
    "url": {"type": "string", "description": "http(s) URL to open"}},
    "required": ["url"]}
_EMPTY_SCHEMA: dict = {"type": "object", "properties": {}}
_QUERY_SCHEMA = {"type": "object", "properties": {
    "query": {"type": "string", "description": "text to find on the page"}},
    "required": ["query"]}
_TARGET_SCHEMA = {"type": "object", "properties": {
    "target": {"type": "string", "description": "CSS selector or visible text"}},
    "required": ["target"]}
_TYPE_SCHEMA = {"type": "object", "properties": {
    "target": {"type": "string", "description": "CSS selector of the field"},
    "text": {"type": "string", "description": "text to type"}},
    "required": ["target", "text"]}


def browser_tools(session: BrowserSession | None = None) -> list[Tool]:
    """Risk-classified browser tools (READ navigate/read/find; SEND click/type)."""
    s = session or _SESSION
    return [
        Tool("browser_navigate", RiskClass.READ,
             "Open a web page and read its text",
             lambda url="", **k: s.navigate(url), parameters=_URL_SCHEMA),
        Tool("browser_read", RiskClass.READ,
             "Read the text of the currently open page",
             lambda **k: s.read(), parameters=_EMPTY_SCHEMA),
        Tool("browser_find", RiskClass.READ,
             "Find text on the currently open page",
             lambda query="", **k: s.find(query), parameters=_QUERY_SCHEMA),
        Tool("browser_click", RiskClass.SEND,
             "Click an element on the page (consequential)",
             lambda target="", **k: s.click(target), parameters=_TARGET_SCHEMA),
        Tool("browser_type", RiskClass.SEND,
             "Type text into a field on the page (consequential)",
             lambda target="", text="", **k: s.type_text(target, text),
             parameters=_TYPE_SCHEMA),
    ]
