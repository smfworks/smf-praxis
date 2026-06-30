"""Evolutionary self-improvement for skills (Phase C / G5).

"Grows with you": Praxis already *creates* skills and *quarantines* bad ones;
this closes the loop by *optimizing* the skills it keeps — a dependency-free,
GEPA-style (reflective, fitness-guided) optimizer.

How it works for a target skill:
1. **Fitness** is measured offline and deterministically: how well the skill's
   ``trigger`` retrieves for the goals it was actually used on (from the governed
   ``skill_outcomes`` history). A skill whose trigger doesn't surface it for the
   goals people use it on is a weak skill; a better trigger raises recall.
2. **Mutation** proposes candidate triggers/bodies. With a real LLM it reflects
   on the skill + its failure/usage history and rewrites; offline it falls back
   to deterministic heuristic mutations (keyword enrichment from real goals), so
   the optimizer works with no API key.
3. **Selection** keeps the best-fitness candidate that also passes **guardrails**.

Guardrails (a candidate is rejected unless ALL hold):
* security scan clean (no injected/dangerous content)
* size caps (trigger <= 200 chars, body <= 8 KB)
* semantic preservation (body keyword overlap with the original stays high)
* fitness strictly improves over the current skill

CRUCIALLY this is **propose-only**: ``evolve_skill`` returns a :class:`Proposal`
with a diff; nothing is written until a human applies it (``apply_proposal``),
mirroring the "PR review, never direct commit" rule.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

_MAX_TRIGGER = 200
_MAX_BODY = 8192
_MIN_SEMANTIC_OVERLAP = 0.5   # candidate body must keep >=50% of original keywords

# Common words that carry no retrieval signal — excluded from keyword extraction
# so trigger enrichment surfaces meaningful terms, not filler.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "out", "new",
    "all", "any", "are", "was", "has", "have", "will", "your", "our", "their",
    "its", "his", "her", "them", "they", "you", "use", "using", "via", "get",
    "set", "run", "now", "can", "may", "should", "would", "could", "also",
    "then", "than", "when", "what", "who", "how", "why", "where",
}


@dataclass
class Candidate:
    trigger: str
    body: str
    fitness: float = 0.0
    source: str = "heuristic"   # "llm" | "heuristic"


@dataclass
class Proposal:
    skill_name: str
    current_trigger: str
    current_body: str
    new_trigger: str
    new_body: str
    current_fitness: float
    new_fitness: float
    source: str
    rationale: str = ""
    rejected: list[str] = field(default_factory=list)

    @property
    def improves(self) -> bool:
        return self.new_fitness > self.current_fitness

    def diff(self) -> str:
        cur = f"trigger: {self.current_trigger}\n\n{self.current_body}"
        new = f"trigger: {self.new_trigger}\n\n{self.new_body}"
        return "\n".join(difflib.unified_diff(
            cur.splitlines(), new.splitlines(),
            fromfile=f"{self.skill_name} (current)",
            tofile=f"{self.skill_name} (proposed)", lineterm=""))

    def summary(self) -> str:
        arrow = "improves" if self.improves else "no gain"
        return (f"{self.skill_name}: fitness {self.current_fitness:.3f} -> "
                f"{self.new_fitness:.3f} ({arrow}, via {self.source})")


def _keywords(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", (text or "").lower())
            if w not in _STOPWORDS}


def _semantic_overlap(original: str, candidate: str) -> float:
    a, b = _keywords(original), _keywords(candidate)
    if not a:
        return 1.0
    return len(a & b) / len(a)


# --------------------------------------------------------------------- fitness
def trigger_fitness(trigger: str, goals: list[str]) -> float:
    """Fraction of historical goals whose keywords overlap the trigger.

    A proxy for "does this trigger surface the skill for the work it's used on".
    Deterministic, offline, and monotonic in trigger relevance.
    """
    if not goals:
        return 0.0
    tk = _keywords(trigger)
    if not tk:
        return 0.0
    hits = 0.0
    for g in goals:
        gk = _keywords(g)
        if gk and (tk & gk):
            # weight by overlap fraction so a richer match scores higher
            hits += len(tk & gk) / max(len(gk), 1)
    return hits / len(goals)


def _used_goals(library, skill_name: str, limit: int = 50) -> list[str]:
    """Goals this skill was recorded against in governed outcome history."""
    if library is None or getattr(library, "rag", None) is None:
        return []
    store = library.rag.store
    try:
        rows = store.list_skill_outcomes(skill_name, limit=limit)
    except Exception:  # noqa: BLE001 - older store
        return []
    return [r.get("goal", "") for r in rows if r.get("goal")]


# ------------------------------------------------------------------ mutation
def _heuristic_candidates(skill, goals: list[str]) -> list[Candidate]:
    """Offline mutation: enrich the trigger with salient keywords drawn from the
    goals the skill is actually used on (no LLM needed)."""
    goal_kw: dict[str, int] = {}
    for g in goals:
        for w in _keywords(g):
            goal_kw[w] = goal_kw.get(w, 0) + 1
    trig_kw = _keywords(skill.trigger)
    # most frequent goal keywords not already in the trigger
    extra = [w for w, _ in sorted(goal_kw.items(), key=lambda kv: -kv[1])
             if w not in trig_kw][:5]
    cands: list[Candidate] = []
    if extra:
        enriched = (skill.trigger.rstrip(". ") + "; also: " + ", ".join(extra))[:_MAX_TRIGGER]
        cands.append(Candidate(trigger=enriched, body=skill.body, source="heuristic"))
    return cands


def _llm_candidates(skill, goals: list[str], llm) -> list[Candidate]:
    """Reflective mutation via the LLM: read the skill + the goals it's used on,
    propose a sharper trigger and a tightened body."""
    if llm is None:
        return []
    sample = "\n".join(f"- {g}" for g in goals[:10]) or "(no usage history)"
    prompt = (
        "You are optimizing a reusable agent skill so it is retrieved for the "
        "right goals and gives crisp guidance. Keep the same INTENT.\n\n"
        f"Skill name: {skill.name}\n"
        f"Current trigger: {skill.trigger}\n"
        f"Current body:\n{skill.body}\n\n"
        f"Goals this skill was actually used on:\n{sample}\n\n"
        "Return exactly two sections:\n"
        "TRIGGER: <one improved trigger line>\n"
        "BODY: <improved body, same intent, concise>")
    try:
        out = llm.complete(prompt, role="summarizer")
    except Exception:  # noqa: BLE001
        return []
    trig = _section(out, "TRIGGER") or skill.trigger
    body = _section(out, "BODY") or skill.body
    return [Candidate(trigger=trig[:_MAX_TRIGGER], body=body[:_MAX_BODY],
                      source="llm")]


def _section(text: str, label: str) -> str:
    m = re.search(rf"{label}:\s*(.+?)(?=\n[A-Z]+:|\Z)", text or "", re.S)
    return m.group(1).strip() if m else ""


# ------------------------------------------------------------------ guardrails
def _passes_guardrails(skill, cand: Candidate) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if len(cand.trigger) > _MAX_TRIGGER:
        reasons.append("trigger too long")
    if len(cand.body) > _MAX_BODY:
        reasons.append("body too long")
    overlap = _semantic_overlap(skill.body, cand.body)
    if overlap < _MIN_SEMANTIC_OVERLAP:
        reasons.append(f"semantic drift (overlap {overlap:.2f})")
    try:
        from .security_scan import scan_text
        rep = scan_text(cand.trigger + "\n" + cand.body, target="candidate")
        if not rep.clean:
            reasons.append(f"security scan {rep.grade}")
    except Exception:  # noqa: BLE001
        pass
    return (not reasons), reasons


# --------------------------------------------------------------------- driver
def evolve_skill(library, skill_name: str, llm=None) -> Proposal | None:
    """Propose an improved version of a skill. Returns None if the skill is
    unknown or no improving, guardrail-passing candidate is found.

    PROPOSE-ONLY: never writes. Apply with :func:`apply_proposal`.
    """
    skill = library.get(skill_name) if library else None
    if skill is None:
        return None
    goals = _used_goals(library, skill_name)
    base_fitness = trigger_fitness(skill.trigger, goals)

    candidates = _llm_candidates(skill, goals, llm) + \
        _heuristic_candidates(skill, goals)

    best: Candidate | None = None
    rejected: list[str] = []
    for cand in candidates:
        ok, reasons = _passes_guardrails(skill, cand)
        if not ok:
            rejected.append(f"{cand.source}: {', '.join(reasons)}")
            continue
        cand.fitness = trigger_fitness(cand.trigger, goals)
        if cand.fitness > base_fitness and (best is None or cand.fitness > best.fitness):
            best = cand

    if best is None:
        return None
    return Proposal(
        skill_name=skill_name, current_trigger=skill.trigger,
        current_body=skill.body, new_trigger=best.trigger, new_body=best.body,
        current_fitness=base_fitness, new_fitness=best.fitness,
        source=best.source, rejected=rejected,
        rationale=f"trigger enriched from {len(goals)} historical goal(s)")


def apply_proposal(library, proposal: Proposal) -> bool:
    """Apply an approved proposal: bump the skill version and persist. This is the
    only path that writes — called after human approval, never automatically."""
    skill = library.get(proposal.skill_name)
    if skill is None:
        return False
    skill.trigger = proposal.new_trigger
    skill.body = proposal.new_body
    skill.version += 1
    skill.provenance = "evolved"
    library.add(skill)   # re-runs the security scan gate as a backstop
    return True
