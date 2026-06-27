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
from typing import List

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.skills")

# Tolerance (seconds) for the "did this SKILL.md change on disk?" freshness
# check. The stored embedding timestamp comes from time.time(), which on Windows
# is quantized to the ~16 ms system clock tick, while the filesystem mtime is
# finer-grained. Without a margin, a skill embedded in the same tick it was
# written can read as "newer than its own embedding" and get needlessly
# re-embedded on every reload (and flake the no-reembed test). A genuine
# out-of-band edit is seconds newer, so a small margin preserves that signal.
_FRESHNESS_EPSILON_S = 2.0


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
            from .embeddings import EmbeddingClient
            from .rag import Rag
            self.rag = Rag(store, embedder or EmbeddingClient(), ns="skills")
        self._load_disk()

    # ------------------------------------------------------------------- disk
    def _load_disk(self) -> None:
        if not self.root.exists():
            return
        for md in self.root.glob("*/SKILL.md"):
            try:
                sk = Skill.from_markdown(md.read_text(encoding="utf-8"))
                self.skills[sk.name] = sk
                if self.rag is None:
                    continue
                # (Re)embed only when missing or the file changed on disk: avoids
                # re-embedding every skill on each construction (an API call per
                # skill against a real embedder) while still catching edits made
                # directly to a SKILL.md file out of band.
                stored_ts = self.rag.store.doc_latest_ts("skills", sk.name)
                if md.stat().st_mtime > stored_ts + _FRESHNESS_EPSILON_S:
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

    def retrieve(self, goal: str, k: int = 3) -> List[Skill]:
        def active(sk: Skill) -> bool:
            if not sk.enabled:
                return False
            if self.rag is not None:
                meta = self.rag.store.skill_metadata(sk.name)
                if meta and meta.get("quarantined"):
                    return False
            return True

        if self.rag is not None and self.rag.store.count_vectors("skills"):
            hits = self.rag.retrieve(goal, k=k, ns="skills")
            out = [
                self.skills[h.source] for h in hits
                if h.source in self.skills and active(self.skills[h.source])
            ]
            if out:
                return out
        # Lexical fallback over name/trigger/body via BM25 (no embedder needed).
        from .bm25 import BM25Index
        active_skills = [sk for sk in self.skills.values() if active(sk)]
        if not active_skills:
            return []
        index = BM25Index.build(
            (sk.name, f"{sk.name} {sk.trigger} {sk.body}") for sk in active_skills)
        by_name = {sk.name: sk for sk in active_skills}
        return [by_name[name] for name, _ in index.search(goal, k=k)
                if name in by_name]

    def recall_context(self, goal: str, k: int = 2, max_chars: int = 900) -> str:
        """A compact block of the most relevant learned procedures (or ``''``).

        Suitable for prepending to a chat system prompt so the agent applies its
        own distilled skills to a recurring task — the procedural-memory half of
        the self-improvement loop.
        """
        blocks: list[str] = []
        used = 0
        for sk in self.retrieve(goal, k=k):
            block = f"### {sk.name}\nWhen: {sk.trigger}\n{sk.body}".strip()
            if used + len(block) > max_chars:
                break
            blocks.append(block)
            used += len(block)
        if not blocks:
            return ""
        return ("Relevant learned procedures (apply if they fit; governance still "
                "applies to every step):\n\n" + "\n\n".join(blocks))

    # -------------------------------------------------------------- outcomes
    def record_outcome(self, skill_name: str, goal: str, outcome: str,
                       score: float | None = None, cycle_id: str = "",
                       notes: str = "") -> None:
        if self.rag is None:
            return
        if outcome not in ("success", "partial", "failure"):
            raise ValueError("outcome must be success, partial, or failure")
        numeric = score if score is not None else {
            "success": 1.0, "partial": 0.5, "failure": 0.0,
        }[outcome]
        self.rag.store.record_skill_outcome(
            skill_name, goal, outcome, numeric, cycle_id=cycle_id, notes=notes)

    def metadata(self, skill_name: str) -> dict | None:
        if self.rag is None:
            return None
        return self.rag.store.skill_metadata(skill_name)

    def quarantine_low_quality(self, min_uses: int = 3,
                               threshold: float = 0.4) -> List[str]:
        if self.rag is None:
            return []
        quarantined: list[str] = []
        for meta in self.rag.store.list_skill_metadata():
            if (meta["usage_count"] >= min_uses
                    and meta["quality_score"] < threshold
                    and not meta["quarantined"]):
                self.rag.store.set_skill_quarantine(meta["skill_name"], True)
                quarantined.append(meta["skill_name"])
        return quarantined

    def unquarantine(self, skill_name: str) -> None:
        if self.rag is not None:
            self.rag.store.set_skill_quarantine(skill_name, False)


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
    from .structured import generate_json
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
