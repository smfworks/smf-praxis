"""Real local and web tools for Praxis.

These complement the mock M365 stand-ins. All filesystem writes are restricted to
a configurable working directory (``PRAXIS_WORK_DIR``, defaulting to the current
working directory) so a remote planner cannot escape onto arbitrary host paths.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath


def _work_dir() -> Path:
    base = os.environ.get("PRAXIS_WORK_DIR", os.getcwd())
    return Path(base).resolve()


def _resolve(relative: str) -> Path:
    """Resolve a path strictly inside the work directory.

    Raises ValueError on traversal attempts (../etc/passwd, absolute paths, etc.).
    """
    root = _work_dir()
    raw = Path(relative)
    # Detect absolute paths consistently across operating systems. On Windows,
    # Path("/etc/passwd").is_absolute() is False (no drive), so a POSIX-style
    # absolute path would slip past a naive is_absolute() check. Test every
    # flavor: a leading separator, a Windows drive/UNC root, and a POSIX root.
    if (raw.is_absolute()
            or relative.startswith(("/", "\\"))
            or PureWindowsPath(relative).is_absolute()
            or PureWindowsPath(relative).drive
            or PurePosixPath(relative).is_absolute()):
        raise ValueError(f"absolute paths not allowed: {relative}")
    # resolve() collapses .. segments; check the final path is still under root.
    resolved = (root / raw).resolve()
    # Use path length check + commonpath for portability.
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes work directory: {relative}") from exc
    return resolved


def read_file(name: str, **_kw) -> str:
    """Read text from a file under PRAXIS_WORK_DIR."""
    path = _resolve(name)
    if not path.exists():
        return f"[read_file] '{name}' not found"
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return f"[read_file] '{name}' is not valid UTF-8 text"


def write_file(name: str, content: str, **_kw) -> str:
    """Write text to a file under PRAXIS_WORK_DIR (idempotent; creates dirs)."""
    path = _resolve(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return f"[write_file] wrote {len(content)} chars to '{name}'"


def list_dir(path: str = ".", **_kw) -> str:
    """List files and directories under PRAXIS_WORK_DIR/path."""
    root = _resolve(path)
    if not root.exists():
        return f"[list_dir] '{path}' not found"
    if not root.is_dir():
        return f"[list_dir] '{path}' is not a directory"
    items = sorted(root.iterdir(), key=lambda p: p.name)
    lines = [f"[list_dir] {path}/"]
    for item in items:
        mark = "D" if item.is_dir() else "F"
        lines.append(f"  {mark} {item.name}")
    return "\n".join(lines)


# ------------------------------------------------------------------ web tools
def fetch_url(url: str, **_kw) -> str:
    """Fetch the text content of a URL.

    Uses only the standard library so no extra dependency is required.
    """
    import urllib.error
    import urllib.request
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return f"[fetch_url] unsupported scheme: {parsed.scheme or 'none'}"
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; PraxisAgent/0.13; "
                    "+https://github.com/smfworks/smf-praxis)"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = resp.read(1_000_000)  # 1 MiB cap
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError:
                text = data.decode("utf-8", errors="replace")
            return f"[fetch_url] {len(text)} chars from {url}\n{text[:2000]}"
    except urllib.error.HTTPError as exc:
        return f"[fetch_url] HTTP {exc.code} for {url}"
    except urllib.error.URLError as exc:
        return f"[fetch_url] failed: {exc.reason}"
    except Exception as exc:  # pragma: no cover - defensive
        return f"[fetch_url] error: {exc}"


def search_web(query: str, max_results: int = 5, **_kw) -> str:
    """Search the web via the configured provider.

    Set ``PRAXIS_SEARCH=tavily|brave|serpapi`` (or ``agents.search.provider`` in
    praxis.json) with the provider's API-key env var (``TAVILY_API_KEY`` etc.) for
    real results. Falls back to a generic ``PRAXIS_SEARCH_URL`` (``?q=`` endpoint),
    then to an honest placeholder when nothing is configured.
    """
    from .search import web_search

    results = web_search(query, max_results=max_results)
    if results is not None:
        if not results:
            return f"[search_web] no results for {query!r}."
        lines = []
        for i, r in enumerate(results, 1):
            snippet = " ".join((r.snippet or "").split())
            if len(snippet) > 280:
                snippet = snippet[:277] + "..."
            lines.append(f"{i}. {r.title} — {snippet}\n   {r.url}")
        return (f"[search_web] {len(results)} result(s) for {query!r}:\n"
                + "\n".join(lines))

    endpoint = os.environ.get("PRAXIS_SEARCH_URL")
    if endpoint:
        import urllib.parse
        return fetch_url(f"{endpoint}?q={urllib.parse.quote(query)}")
    return (
        f"[search_web] no search backend configured for query: {query!r}. "
        "Set PRAXIS_SEARCH=tavily|brave|serpapi with the provider's API key "
        "(e.g. TAVILY_API_KEY), or PRAXIS_SEARCH_URL for a custom endpoint."
    )
