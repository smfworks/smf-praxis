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

import queue
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
    """Stdlib fetch used when Playwright is unavailable; SSRF-gated like KB."""
    from .wiki_safe import UnsafeSourceError, validate_uri
    from .wiki_safe import fetch_url as _safe_fetch
    try:
        validate_uri(url)
        return _safe_fetch(
            url, timeout=20.0, max_bytes=2_000_000,
            user_agent="PraxisAgent/0.19 (+browser)")
    except UnsafeSourceError as exc:
        return f"<title>error</title>blocked {url}: {exc}"
    except OSError as exc:
        return f"<title>error</title>error fetching {url}: {exc}"


class BrowserSession:
    """A stateful browsing session. Uses Playwright when available, else a
    dependency-free read-only fetch for navigation/reading."""

    def __init__(self, allow_playwright: bool = True) -> None:
        self._page: Any = None
        self._pw: Any = None
        self._browser: Any = None
        self._lock = threading.Lock()
        self._jobs: Any = None
        self._worker: Any = None
        self.allow_playwright = allow_playwright
        self.url = ""
        self.title = ""
        self.text = ""

    def _on_worker(self, fn: Any) -> Any:
        """Run ``fn`` on a single dedicated thread that owns all Playwright
        objects. The sync API is thread-affine and the daemon serves each
        connection on its own thread, so every Playwright call must be
        marshalled to one owning thread."""
        if self._worker is None:
            self._jobs = queue.Queue()
            self._worker = threading.Thread(target=self._worker_loop, daemon=True)
            self._worker.start()
        box: dict = {}
        done = threading.Event()
        self._jobs.put((fn, box, done))
        done.wait()
        if "exc" in box:
            raise box["exc"]
        return box.get("result")

    def _worker_loop(self) -> None:
        while True:
            fn, box, done = self._jobs.get()
            try:
                box["result"] = fn()
            except Exception as exc:
                box["exc"] = exc
            finally:
                done.set()

    def _start_playwright(self) -> bool:
        """Start Playwright (runs on the worker thread)."""
        if self._page is not None:
            return True
        try:
            from playwright.sync_api import sync_playwright  # type: ignore
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_page()
            return True
        except Exception:  # package missing or no browser binaries -> fallback
            self._page = self._browser = self._pw = None
            return False

    def _have_browser(self) -> bool:
        if not self.allow_playwright:
            return False
        return bool(self._on_worker(self._start_playwright))

    def navigate(self, url: str) -> str:
        from .wiki_safe import UnsafeSourceError, validate_uri
        try:
            validate_uri(url)
        except UnsafeSourceError as exc:
            return f"[browser] blocked: {exc}"
        with self._lock:
            if self._have_browser():
                def _go() -> tuple[str, str, str]:
                    self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    final = self._page.url
                    # Re-check after Playwright follows redirects.
                    try:
                        validate_uri(final)
                    except UnsafeSourceError as exc:
                        return (final, "blocked", f"redirect blocked: {exc}")
                    return (final, self._page.title(),
                            self._page.inner_text("body")[:20000])
                self.url, self.title, self.text = self._on_worker(_go)
                if self.title == "blocked" or (
                        isinstance(self.text, str)
                        and self.text.startswith("redirect blocked:")):
                    return f"[browser] blocked after redirect: {self.text}"
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
            if not self._have_browser():
                return ("[browser] interaction requires the optional [browser] "
                        "extra (pip install praxis-agent[browser])")

            def _click() -> tuple[str, str]:
                self._page.click(target, timeout=10000)
                return (self._page.url, self._page.inner_text("body")[:20000])
            self.url, self.text = self._on_worker(_click)
        return f"[browser] clicked {target!r}; now at {self.url}"

    def type_text(self, target: str, text: str) -> str:
        with self._lock:
            if not self._have_browser():
                return ("[browser] interaction requires the optional [browser] "
                        "extra (pip install praxis-agent[browser])")
            self._on_worker(lambda: self._page.fill(target, text))
        return f"[browser] typed {len(text)} chars into {target!r}"

    def close(self) -> None:
        with self._lock:
            if self._worker is None:
                return

            def _close() -> None:
                try:
                    if self._browser is not None:
                        self._browser.close()
                    if self._pw is not None:
                        self._pw.stop()
                except Exception:
                    pass
                self._page = self._browser = self._pw = None
            try:
                self._on_worker(_close)
            except Exception:
                pass


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
