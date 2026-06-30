"""First-run bootstrap — make a fresh install immediately usable.

Provisions sane defaults so the dashboard and CLI work on first boot without the
user having to discover hidden config:

* memory recall and skill recall are enabled by default,
* a starter knowledge namespace is seeded with the bundled Praxis overview so
  grounded ``ask`` returns something on the very first query instead of an empty
  knowledge base.

Everything here is idempotent: it only writes a default when the key is absent,
and it seeds the starter docs only once (tracked by a marker in the config), so
re-running ``praxis onboard`` or restarting the daemon never duplicates work or
overrides a user's explicit choices.
"""
from __future__ import annotations

from pathlib import Path

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.bootstrap")

_SEED_NS = "praxis-docs"
_SEED_MARKER = "seededDocs"


def _seed_doc() -> Path:
    return Path(__file__).resolve().parent / "seed" / "praxis_overview.md"


def ensure_defaults() -> dict:
    """Write first-run defaults into praxis.json if they are not already set.

    Returns a summary of what changed. Never overrides an existing explicit
    value — only fills in absent keys.
    """
    conf = cfg.load_config()
    agents = conf.setdefault("agents", {})
    changed: list[str] = []
    for key, default in (("memoryRecall", True), ("skillRecall", True),
                         ("hybridRetrieval", True)):
        if key not in agents:
            agents[key] = default
            changed.append(key)
    if changed:
        cfg.save_config(conf)
        _log.info("bootstrap: set defaults %s", ", ".join(changed))
    return {"defaults_set": changed}


def seed_knowledge(store=None) -> dict:
    """Seed the bundled Praxis overview into a starter knowledge namespace, once.

    Idempotent: guarded by a config marker so it runs exactly once per profile.
    Safe to call on every startup. Returns a summary dict.
    """
    conf = cfg.load_config()
    if conf.get(_SEED_MARKER):
        return {"seeded": False, "reason": "already seeded"}
    doc = _seed_doc()
    if not doc.exists():
        return {"seeded": False, "reason": "seed doc missing"}
    try:
        from .persistence import Store
        from .rag import Rag
        s = store or Store.open()
        rag = Rag(s)
        text = doc.read_text(encoding="utf-8")
        n = rag.ingest_text(text, source="praxis-overview", kind="document",
                            provenance="bundled:praxis_overview.md", ns=_SEED_NS)
    except Exception as exc:
        _log.warning("bootstrap: could not seed knowledge: %s", exc)
        return {"seeded": False, "reason": str(exc)}
    conf[_SEED_MARKER] = True
    cfg.save_config(conf)
    _log.info("bootstrap: seeded %d starter chunk(s) into ns=%s", n, _SEED_NS)
    return {"seeded": True, "chunks": n, "ns": _SEED_NS}


def run(store=None) -> dict:
    """Run the full first-run bootstrap (defaults + starter knowledge)."""
    return {"defaults": ensure_defaults(), "knowledge": seed_knowledge(store)}
