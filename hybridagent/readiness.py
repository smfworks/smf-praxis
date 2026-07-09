"""First-run readiness checks — the source of truth for `praxis doctor` and the
dashboard readiness banner.

Each check returns a :class:`Check` with a status (``ok`` / ``warn`` / ``off``)
and a short, actionable hint so a fresh install can see *exactly* what is and
isn't wired, instead of discovering silent failures only when a query fails.

Statuses:
    ok    — feature is configured and working
    warn  — feature needs attention (e.g. no model, empty KB)
    off   — feature is intentionally available but not configured (not an error)

The whole point: an out-of-the-box install should be able to answer
"can I research? can I query a knowledge base? is my memory on?" at a glance.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass

from . import config as cfg


@dataclass
class Check:
    key: str
    label: str
    status: str          # ok | warn | off
    detail: str
    hint: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def _check_model() -> Check:
    model = cfg.get_default_model()
    if model:
        return Check("model", "Language model", "ok", model)
    return Check(
        "model", "Language model", "warn",
        "offline deterministic mock (no real provider configured)",
        "Run `praxis onboard` or use the dashboard model picker to connect a "
        "provider (OpenAI, Anthropic, Ollama, OpenRouter, ...).")


def _check_embed() -> Check:
    embed = cfg.get_embed_model()
    if embed:
        return Check("embed", "Embedding model", "ok", embed)
    return Check(
        "embed", "Embedding model", "ok",
        "local deterministic embedder (keyless)",
        "Hybrid retrieval works offline with the built-in embedder; set "
        "agents.defaults.embedModel for a hosted embedder.")


def _check_memory() -> Check:
    conf = cfg.load_config().get("agents", {})
    on = conf.get("memoryRecall", True)
    if on:
        return Check("memory", "Persistent memory", "ok",
                     "recall enabled; memory persists in the local store")
    return Check("memory", "Persistent memory", "off",
                 "memory recall disabled",
                 "Set agents.memoryRecall=true (or unset PRAXIS_MEMORY_RECALL=0).")


def _check_skills() -> Check:
    conf = cfg.load_config().get("agents", {})
    on = conf.get("skillRecall", True)
    if on:
        return Check("skills", "Skill recall", "ok",
                     "learned skills are recalled into the governed loop")
    return Check("skills", "Skill recall", "off", "skill recall disabled",
                 "Set agents.skillRecall=true to inject learned procedures.")


def _check_search() -> Check:
    """Web research readiness. Keyless DuckDuckGo is the default fallback, so
    research works out of the box; a configured provider is an upgrade."""
    try:
        from .search import configured_provider
        prov = configured_provider()
    except Exception:
        prov = None
    if prov:
        return Check("search", "Web research", "ok",
                     f"provider: {prov}")
    return Check(
        "search", "Web research", "ok",
        "keyless DuckDuckGo default",
        "For higher-quality results set PRAXIS_SEARCH=tavily|brave|serpapi with "
        "the provider's API key, or configure it in the dashboard.")


def _check_wiki(store=None) -> Check:
    """Knowledge base / LLM-wiki readiness: are any RAG repositories registered
    and do they hold indexed content?"""
    try:
        from .persistence import Store
        from .rag import Rag
        from .wiki import KBSourceManager
        s = store or Store.open()
        sources = KBSourceManager(s).list(enabled=None)
        rag = Rag(s)
        try:
            chunks = sum(rag.stats(ns=ns).get("chunks", 0)
                         for ns in s.list_namespaces())
        except Exception:
            chunks = rag.stats().get("chunks", 0)
    except Exception as exc:
        return Check("wiki", "Knowledge base", "warn",
                     f"could not read knowledge base: {exc}")
    n = len(sources)
    if chunks and not n:
        # Seeded/ingested content exists (e.g. the bundled starter docs or
        # `praxis ingest`) even though no managed wiki source is registered.
        return Check("wiki", "Knowledge base", "ok",
                     f"{chunks} indexed chunk(s) available")
    if n and chunks:
        return Check("wiki", "Knowledge base", "ok",
                     f"{n} source(s), {chunks} indexed chunk(s)")
    if n and not chunks:
        return Check("wiki", "Knowledge base", "warn",
                     f"{n} source(s) registered but nothing indexed yet",
                     "Refresh sources from the Knowledge panel or "
                     "`praxis wiki-refresh`.")
    return Check("wiki", "Knowledge base", "off",
                 "no knowledge sources registered",
                 "Add a folder, file, or URL in the dashboard Knowledge panel "
                 "or `praxis wiki-add <uri>` / `praxis ingest <file>`.")



def _check_budget(store=None) -> Check:
    """Spend cap: set = controlled; unset = unlimited (warn for production)."""
    try:
        if store is None:
            from .persistence import Store
            store = Store.open()
        b = store.get_budget()
    except Exception as exc:  # noqa: BLE001
        return Check("budget", "Spend budget", "off", f"unavailable: {exc}")
    limit = float(b.get("limit_usd") or 0)
    spent = float(b.get("spent_usd") or 0)
    if limit > 0 and spent >= limit:
        return Check(
            "budget", "Spend budget", "warn",
            f"cap reached (${spent:.4f} / ${limit:.2f}) — inference is blocked",
            "Raise or reset the budget in Inference Control / `praxis budget`.")
    if limit > 0:
        return Check(
            "budget", "Spend budget", "ok",
            f"${spent:.4f} spent of ${limit:.2f} cap (hard-stop enforced)")
    return Check(
        "budget", "Spend budget", "off",
        "no cap set (unlimited spend)",
        "Set a USD cap in the Inference panel or `praxis budget set 5`.")


def _check_sandbox() -> Check:
    """Report the execution-isolation backend (G6)."""
    try:
        from .sandbox import backend_status
        st = backend_status()
    except Exception as exc:  # noqa: BLE001
        return Check("sandbox", "Execution sandbox", "off", f"unavailable: {exc}")
    eff = st["effective"]
    if eff == "docker":
        return Check("sandbox", "Execution sandbox", "ok",
                     "Docker isolation active (network="
                     f"{st['network']}, image={st['image']})")
    if st.get("configured") in ("docker", "auto") and not st.get("docker_available"):
        return Check("sandbox", "Execution sandbox", "warn",
                     "auto/docker prefers Docker but it is unavailable; using local",
                     "Start Docker for isolation, or set agents.sandbox.backend=local.")
    return Check("sandbox", "Execution sandbox", "off",
                 "local backend (path-confined; no process/network isolation)",
                 "Docker auto-selects when available (agents.sandbox.backend=auto).")


def run_checks(store=None) -> list[Check]:
    """Run every readiness check. Order is the display order."""
    return [
        _check_model(),
        _check_memory(),
        _check_search(),
        _check_wiki(store),
        _check_embed(),
        _check_skills(),
        _check_sandbox(),
        _check_budget(store),
    ]


def readiness(store=None) -> dict:
    """Aggregate readiness payload for the dashboard and `praxis doctor`."""
    checks = run_checks(store)
    counts = {"ok": 0, "warn": 0, "off": 0}
    for c in checks:
        counts[c.status] = counts.get(c.status, 0) + 1
    # The install is "ready" when nothing is in a warn state; off items are
    # available-but-not-configured and don't block first use.
    return {
        "ready": counts.get("warn", 0) == 0,
        "counts": counts,
        "checks": [c.to_dict() for c in checks],
    }


def render(store=None) -> str:
    """Human-readable readiness report for `praxis doctor`."""
    rep = readiness(store)
    marks = {"ok": "[ ok ]", "warn": "[warn]", "off": "[ -- ]"}
    lines = ["Praxis readiness:"]
    for c in rep["checks"]:
        line = f"  {marks.get(c['status'], '[????]')} {c['label']}: {c['detail']}"
        lines.append(line)
        if c["status"] != "ok" and c.get("hint"):
            lines.append(f"          -> {c['hint']}")
    c = rep["counts"]
    lines.append("")
    lines.append(f"  {c.get('ok', 0)} ok, {c.get('warn', 0)} need attention, "
                 f"{c.get('off', 0)} available but off")
    return "\n".join(lines)
