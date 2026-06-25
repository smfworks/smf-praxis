"""Skills library — persistent, retrievable, reusable procedures.

A Hermes-grade skills layer: each skill is a named, triggerable procedure stored
as a ``SKILL.md`` file (YAML-ish frontmatter + markdown body) under
``~/.praxis/skills/<slug>/``, and indexed in the vector store (namespace
``skills``) so the *relevant* skills can be retrieved for a goal and folded into
perception/planning.

Governance: writing a skill changes future behavior, so it is a *consequential*
act — the ``praxis learn`` command distills a **draft** autonomously but only
persists it after human approval (``autonomy for preparation, approval for
consequence``). Distillation itself is offline-deterministic in mock mode and
LLM-driven (structured JSON) in real mode.
"""
from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.skills")


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or "skill"


def _now() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%d")


# --------------------------------------------------------- frontmatter (no deps)
def _dump_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


def _parse_value(raw: str):
    raw = raw.strip()
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [x.strip() for x in inner.split(",") if x.strip()] if inner else []
    if raw in ("true", "false"):
        return raw == "true"
    if raw.isdigit():
        return int(raw)
    return raw


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---"):
        return {}, text
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}, text
    meta: dict = {}
    for line in parts[1].strip().splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = _parse_value(v)
    return meta, parts[2].lstrip("\n")


@dataclass
class Skill:
    name: str
    trigger: str
    body: str = ""
    kind: str = "skill"
    version: int = 1
    enabled: bool = True
    provenance: str = "learn"
    created: str = field(default_factory=_now)
    tags: list[str] = field(default_factory=list)

    def slug(self) -> str:
        return _slug(self.name)

    def to_markdown(self) -> str:
        meta = {
            "name": self.name, "trigger": self.trigger, "kind": self.kind,
            "version": self.version, "enabled": self.enabled,
            "provenance": self.provenance, "created": self.created,
            "tags": self.tags,
        }
        front = "\n".join(f"{k}: {_dump_value(v)}" for k, v in meta.items())
        return f"---\n{front}\n---\n\n# {self.name}\n\n{self.body}\n"

    @classmethod
    def from_markdown(cls, text: str) -> "Skill":
        meta, body = _split_frontmatter(text)
        # Strip a leading "# name" heading from the body if present.
        body = re.sub(r"^#\s+.*\n+", "", body, count=1).strip()
        return cls(
            name=meta.get("name", "unnamed"),
            trigger=meta.get("trigger", ""),
            body=body, kind=meta.get("kind", "skill"),
            version=int(meta.get("version", 1)),
            enabled=bool(meta.get("enabled", True)),
            provenance=meta.get("provenance", "learn"),
            created=meta.get("created", _now()),
            tags=meta.get("tags", []) or [],
        )


class SkillLibrary:
    def __init__(self, store=None, embedder=None, root: Path | None = None) -> None:
        self.root = root or (cfg.home_dir() / "skills")
        self.skills: dict[str, Skill] = {}
        self.rag = None
        if store is not None:
            from .rag import Rag
            from .embeddings import EmbeddingClient
            self.rag = Rag(store, embedder or EmbeddingClient(), ns="skills")
        self._load_disk()

    # ------------------------------------------------------------------- disk
    def _load_disk(self) -> None:
        if not self.root.exists():
            return
        # Only (re)embed skills that aren't already in the vector store, so
        # constructing a library (every CLI invocation) doesn't re-embed every
        # skill — which against a real embedding model means N API calls per run.
        indexed = set(self.rag.store.doc_ids("skills")) if self.rag else set()
        for md in self.root.glob("*/SKILL.md"):
            try:
                sk = Skill.from_markdown(md.read_text(encoding="utf-8"))
                self.skills[sk.name] = sk
                if sk.name not in indexed:
                    self._index(sk)
            except Exception as exc:
                _log.warning("failed to load skill %s: %s", md, exc)

    def path_for(self, skill: Skill) -> Path:
        return self.root / skill.slug() / "SKILL.md"

    def _index(self, skill: Skill) -> None:
        if self.rag is not None:
            self.rag.ingest_text(f"{skill.name}\n{skill.trigger}\n{skill.body}",
                                 source=skill.name, kind="skill",
                                 provenance=f"skill:{skill.provenance}", ns="skills")

    # --------------------------------------------------------------- mutation
    def add(self, skill: Skill) -> Path:
        path = self.path_for(skill)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(skill.to_markdown(), encoding="utf-8")
        self.skills[skill.name] = skill
        self._index(skill)
        _log.info("saved skill '%s' -> %s", skill.name, path)
        return path

    def remove(self, name: str) -> bool:
        sk = self.skills.pop(name, None)
        if sk is None:
            return False
        path = self.path_for(sk)
        if path.exists():
            path.unlink()
            try:
                path.parent.rmdir()
            except OSError:
                pass
        if self.rag is not None:
            self.rag.store.delete_doc("skills", name)
        return True

    # ---------------------------------------------------------------- reading
    def get(self, name: str) -> Skill | None:
        return self.skills.get(name)

    def list(self) -> list[Skill]:
        return sorted(self.skills.values(), key=lambda s: s.name)

    def retrieve(self, goal: str, k: int = 3) -> list[Skill]:
        if self.rag is not None and self.rag.store.count_vectors("skills"):
            hits = self.rag.retrieve(goal, k=k, ns="skills")
            out = [self.skills[h.source] for h in hits if h.source in self.skills]
            if out:
                return out
        # Lexical fallback over triggers/names.
        q = set(re.findall(r"[a-z0-9]+", goal.lower()))
        scored = []
        for sk in self.skills.values():
            toks = set(re.findall(r"[a-z0-9]+", f"{sk.name} {sk.trigger}".lower()))
            overlap = len(q & toks)
            if overlap:
                scored.append((overlap, sk))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [sk for _, sk in scored[:k]]


# ----------------------------------------------------------------- distillation
def distill_skill(llm, goal: str, trace: list[str] | None = None,
                  name: str | None = None) -> Skill:
    """Distill a reusable Skill draft from a goal and a (planned) action trace."""
    trace = trace or []
    if getattr(llm, "_effective_mode", lambda: "mock")() == "real":
        try:
            return _distill_real(llm, goal, trace, name)
        except Exception as exc:
            _log.warning("LLM skill distillation failed (%s); using template", exc)
    return _distill_template(goal, trace, name)


def _distill_template(goal: str, trace: list[str], name: str | None) -> Skill:
    short = goal.strip()[:48]
    nm = name or _slug(short)
    steps = trace or ["perceive relevant context", "draft outputs autonomously",
                      "route any send/destructive step through approval"]
    body = ("Use this when the goal resembles: "
            f"\"{short}\".\n\nSteps:\n"
            + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
            + "\n\nGovernance: reads/drafts run autonomously; sends/deletes are "
              "held for human approval.")
    return Skill(name=nm, trigger=f"goals like '{short}'", body=body,
                 provenance="learn:template")


def _distill_real(llm, goal: str, trace: list[str], name: str | None) -> Skill:
    from .grounding import generate_json
    trace_text = "\n".join(f"- {t}" for t in trace) or "(no trace)"
    prompt = (
        f"Goal: {goal}\n\nObserved/planned steps:\n{trace_text}\n\n"
        "Distill a single reusable skill. Return JSON: "
        '{"name": "kebab-case-name", "trigger": "when to use this", '
        '"steps": ["step 1", "step 2"]}')
    obj = generate_json(llm, prompt, ["name", "trigger", "steps"], role="planner")
    steps = obj.get("steps") or []
    body = ("Steps:\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
            + "\n\nGovernance: reads/drafts autonomous; sends/deletes need approval.")
    return Skill(name=name or _slug(obj["name"]), trigger=str(obj["trigger"]),
                 body=body, provenance="learn:llm")
