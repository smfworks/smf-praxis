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
        from .errors import agent_error
        raise ValueError(agent_error(
            what=f"absolute paths not allowed: {relative}",
            why="filesystem tools sandbox to PRAXIS_WORK_DIR for safety",
            fix="use a path relative to the work directory, e.g. 'data/file.txt'",
        ))
    # resolve() collapses .. segments; check the final path is still under root.
    resolved = (root / raw).resolve()
    # Use path length check + commonpath for portability.
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        from .errors import agent_error
        raise ValueError(agent_error(
            what=f"path escapes work directory: {relative}",
            why="the resolved path lands outside PRAXIS_WORK_DIR after "
                "collapsing '..' segments",
            fix="use a path that stays under the work directory root; "
                "remove '..' segments",
        )) from exc
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
def delegate(goal: str = "", role: str = "", **_kw) -> str:
    """Delegate a sub-goal to a scoped, isolated subagent and return its result.

    The subagent is an ordinary governed PraxisAgent with a narrowed tool set
    (chosen by ``role`` or auto-routed from the goal). It runs under the SAME
    governance spine — its SEND/DESTRUCTIVE tool calls are still held for
    approval — and recursion is depth-capped by the orchestrator. Use this to
    fan a complex task into a focused worker without flooding the main loop.
    """
    goal = (goal or "").strip()
    if not goal:
        return "[delegate] a goal is required"
    try:
        from .orchestrator import Orchestrator
        from .persistence import Store
        orch = Orchestrator(Store.open())
        run = orch.run(goal, role=(role or None))
    except Exception as exc:
        return f"[delegate] failed: {exc}"
    return (f"[delegate] subagent role={run.role} -> {run.status} "
            f"(run {run.run_id}); inspect with 'praxis subagents'")


def generate_image(prompt: str = "", size: str = "1024x1024", **_kw) -> str:
    """Generate an image from a text prompt via an OpenAI-compatible image API.

    DRAFT risk: produces an artifact reference (URL/b64), sends nothing anywhere.
    Requires an image-capable provider key (OPENAI_API_KEY or XAI_API_KEY);
    returns an honest note when none is configured.
    """
    import json as _json
    import os
    import urllib.request

    prompt = (prompt or "").strip()
    if not prompt:
        return "[generate_image] a prompt is required"
    candidates = [
        ("OPENAI_API_KEY", "https://api.openai.com/v1/images/generations", "gpt-image-1"),
        ("XAI_API_KEY", "https://api.x.ai/v1/images/generations", "grok-2-image"),
    ]
    for env, url, model in candidates:
        key = os.environ.get(env)
        if not key:
            continue
        try:
            body = _json.dumps({"model": model, "prompt": prompt, "size": size,
                                "n": 1}).encode()
            req = urllib.request.Request(
                url, data=body, method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = _json.loads(resp.read().decode())
            item = (data.get("data") or [{}])[0]
            ref = item.get("url") or (item.get("b64_json", "")[:40] + "...(b64)")
            return f"[generate_image] generated via {model}: {ref}"
        except Exception as exc:  # noqa: BLE001
            return f"[generate_image] {model} failed: {exc}"
    return ("[generate_image] no image provider configured "
            "(set OPENAI_API_KEY or XAI_API_KEY)")


def text_to_speech(text: str = "", voice: str = "alloy", **_kw) -> str:
    """Synthesize speech from text via an OpenAI-compatible TTS API, saving an
    mp3 under PRAXIS_WORK_DIR. DRAFT risk: writes a local artifact, sends nothing.
    """
    import json as _json
    import os
    import urllib.request

    text = (text or "").strip()
    if not text:
        return "[text_to_speech] text is required"
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return "[text_to_speech] no TTS provider configured (set OPENAI_API_KEY)"
    try:
        body = _json.dumps({"model": "gpt-4o-mini-tts", "input": text[:4000],
                            "voice": voice}).encode()
        req = urllib.request.Request(
            "https://api.openai.com/v1/audio/speech", data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"})
        out_path = _resolve(f"tts_{abs(hash(text)) % 10**8}.mp3")
        with urllib.request.urlopen(req, timeout=120) as resp:
            out_path.write_bytes(resp.read())
        return f"[text_to_speech] wrote {out_path.name} ({voice})"
    except Exception as exc:  # noqa: BLE001
        return f"[text_to_speech] failed: {exc}"


def run_shell(command: str = "", timeout: int = 60, **_kw) -> str:
    """Execute a shell command through the configured isolation backend.

    Runs via hybridagent.sandbox, so the command is confined by whatever backend
    the operator configured (local path-confinement, Docker container, SSH host,
    or a serverless sandbox CLI) — isolation is enforced by construction, not
    bolted on. DESTRUCTIVE risk: shell is the highest-blast-radius capability, so
    the broker HOLDS it for human approval before it runs.
    """
    command = (command or "").strip()
    if not command:
        return "[run_shell] a command is required"
    try:
        import os as _os

        from .sandbox import run as sandbox_run
        workdir = _os.environ.get("PRAXIS_WORK_DIR", ".")
        res = sandbox_run(command, workdir=workdir, timeout=float(timeout))
    except Exception as exc:  # noqa: BLE001
        return f"[run_shell] failed: {exc}"
    head = f"[run_shell] ({res.backend}) exit={res.exit_code}"
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    parts = [head]
    if out:
        parts.append(out[:2000])
    if err:
        parts.append("stderr: " + err[:500])
    return "\n".join(parts)


def call_agent(target: str = "", goal: str = "", **_kw) -> str:
    """Call another autonomous agent (A2A) to handle a goal and return its result.

    ``target`` is a registered peer name (agents.a2a.peers) or an http(s) base
    URL of an agent exposing the Praxis A2A contract. SEND-risk: outbound agent
    calls are held for approval, since the remote agent is untrusted and may take
    consequential actions of its own.
    """
    import json as _json

    from .a2a_client import call_agent as _call
    target = (target or "").strip()
    goal = (goal or "").strip()
    if not target or not goal:
        return "[call_agent] target and goal are required"
    res = _call(target, goal)
    if "error" in res:
        return f"[call_agent] {res['error']}"
    summary = res.get("summary") or res.get("status") or _json.dumps(res)[:300]
    return f"[call_agent] {target} -> {summary}"


def send_message(target: str = "", text: str = "", **_kw) -> str:
    """Send a message to a configured messaging gateway (Telegram/Slack/Discord/
    webhook/ntfy). ``target`` is '<channel>' or '<channel>:<destination>'.

    SEND-risk: the broker holds it for human approval before it reaches anyone
    (draft-before-send), so the agent can propose notifications without spamming.
    """
    from .gateways import deliver
    if not target or not text:
        return "[send_message] target and text are required"
    res = deliver(target, text)
    return (f"[send_message] {'sent' if res.ok else 'FAILED'} via {res.channel} "
            f"({res.detail})")


def query_knowledge(question: str, k: int = 5, **_kw) -> str:
    """Answer a question grounded in the local knowledge base (RAG repositories).

    Searches every registered knowledge namespace and returns the most relevant
    indexed chunks with their sources, so the agent can ground answers in the
    operator's documents instead of guessing. Returns an honest note when the
    knowledge base is empty.
    """
    try:
        from .persistence import Store
        from .rag import Rag
        rag = Rag(Store.open())
        hits = rag.retrieve_all_ns(question, k=max(1, min(int(k or 5), 10)))
    except Exception as exc:
        return f"[query_knowledge] knowledge base unavailable: {exc}"
    if not hits:
        return ("[query_knowledge] no indexed knowledge yet. Add sources via the "
                "dashboard Knowledge panel or `praxis ingest <file>`.")
    lines = [f"[query_knowledge] {len(hits)} relevant chunk(s) for {question!r}:"]
    for i, h in enumerate(hits, 1):
        snippet = " ".join((h.text or "").split())
        if len(snippet) > 400:
            snippet = snippet[:397] + "..."
        lines.append(f"{i}. ({h.source}) {snippet}")
    return "\n".join(lines)


def fetch_url(url: str, **_kw) -> str:
    """Fetch the text content of a URL.

    Uses only the standard library so no extra dependency is required.
    Blocks private/loopback/metadata hosts (same allowlist as KB ingest) and
    re-validates redirect hops so SSRF via open redirects is closed.
    """
    from .wiki_safe import UnsafeSourceError, validate_uri
    from .wiki_safe import fetch_url as _safe_fetch

    try:
        validate_uri(url)
    except UnsafeSourceError as exc:
        return f"[fetch_url] blocked: {exc}"
    try:
        text = _safe_fetch(
            url, timeout=20.0, max_bytes=1_000_000,
            user_agent=(
                "Mozilla/5.0 (compatible; PraxisAgent/0.19; "
                "+https://github.com/smfworks/smf-praxis)"
            ),
        )
        return f"[fetch_url] {len(text)} chars from {url}\n{text[:2000]}"
    except UnsafeSourceError as exc:
        return f"[fetch_url] blocked: {exc}"
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
