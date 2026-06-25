"""Grounding — make generative steps non-hallucinating.

Three mechanisms, all offline-capable:

* **Cite-or-abstain answering** (:class:`GroundedResponder`) — answers a question
  *only* from retrieved sources, cites each claim ``[S#]``, and returns
  ``INSUFFICIENT_EVIDENCE`` when the sources don't support an answer rather than
  guessing. The offline path is purely *extractive* (it copies supporting
  sentences), so it cannot fabricate; the real path uses a strict system prompt
  at temperature 0.
* **Verification pass** (:meth:`GroundedResponder.verify`) — splits an answer into
  claims and flags any not supported by the sources (lexical overlap offline,
  swappable for an LLM judge).
* **Structured / tool-constrained generation** (:func:`generate_json`,
  :class:`GroundedPlanner`) — forces JSON output and drops any planned step that
  references a tool outside the registry, so the planner can never invent tools.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .llm import LLMClient
from .planner import Plan, Planner, Step
from .rag import RetrievedChunk
from .router import classify_sensitivity

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_SENT_RE = re.compile(r"(?<=[.!?])\s+")
_CITE_RE = re.compile(r"\[S(\d+)\]")
ABSTAIN = "INSUFFICIENT_EVIDENCE"


def _tok(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENT_RE.split((text or "").strip()) if s.strip()]


@dataclass
class VerificationResult:
    supported: bool
    unsupported_claims: list[str] = field(default_factory=list)
    checked: int = 0


@dataclass
class GroundedAnswer:
    text: str
    citations: list[str] = field(default_factory=list)
    abstained: bool = False
    verification: VerificationResult | None = None
    sources_used: list[int] = field(default_factory=list)
    contradictions: list = field(default_factory=list)


def _render_sources(sources: list[RetrievedChunk]) -> str:
    return "\n".join(f"[S{i}] ({s.source}): {s.text}"
                     for i, s in enumerate(sources, 1))


class GroundedResponder:
    def __init__(self, llm: LLMClient | None = None,
                 support_threshold: float = 0.5) -> None:
        self.llm = llm or LLMClient()
        self.support_threshold = support_threshold

    # ------------------------------------------------------------------ answer
    def answer(self, question: str, sources: list[RetrievedChunk]) -> GroundedAnswer:
        if not sources:
            return GroundedAnswer(
                f"{ABSTAIN} — no sources were retrieved for this question.",
                abstained=True)
        if self.llm._effective_mode() == "real":
            return self._answer_real(question, sources)
        return self._answer_extractive(question, sources)

    def _answer_extractive(self, question: str,
                           sources: list[RetrievedChunk]) -> GroundedAnswer:
        q = _tok(question)
        scored: list[tuple[int, int, str]] = []
        for idx, src in enumerate(sources, 1):
            for sent in _sentences(src.text):
                overlap = len(q & _tok(sent))
                if overlap:
                    scored.append((overlap, idx, sent))
        if not scored:
            return GroundedAnswer(
                f"{ABSTAIN} — retrieved sources do not address the question.",
                abstained=True)
        scored.sort(key=lambda x: x[0], reverse=True)
        top = scored[:3]
        used = sorted({idx for _, idx, _ in top})
        text = " ".join(f"{sent} [S{idx}]" for _, idx, sent in top)
        answer = GroundedAnswer(
            text=text, citations=[sources[i - 1].source for i in used],
            abstained=False, sources_used=used)
        answer.verification = self.verify(text, sources)
        return answer

    def _answer_real(self, question: str,
                     sources: list[RetrievedChunk]) -> GroundedAnswer:
        rendered = _render_sources(sources)
        system = (
            "You are a meticulous analyst. Answer the QUESTION using ONLY the "
            "SOURCES below. Cite every claim with [S#] markers. If the SOURCES "
            f"are insufficient, reply with exactly '{ABSTAIN}'. Never use "
            "knowledge beyond the SOURCES.")
        prompt = f"QUESTION: {question}\n\nSOURCES:\n{rendered}\n\nGrounded answer:"
        out = self.llm.complete(prompt, system, role="general",
                                sensitivity=classify_sensitivity(rendered))
        abstained = out.strip().upper().startswith(ABSTAIN)
        used = sorted({int(m) for m in _CITE_RE.findall(out)
                       if 1 <= int(m) <= len(sources)})
        ans = GroundedAnswer(
            text=out, citations=[sources[i - 1].source for i in used],
            abstained=abstained, sources_used=used)
        if not abstained:
            ans.verification = self.verify(out, sources)
        return ans

    # ------------------------------------------------------------------ verify
    def verify(self, answer_text: str,
               sources: list[RetrievedChunk]) -> VerificationResult:
        source_sets = [_tok(s) for src in sources for s in _sentences(src.text)]
        unsupported: list[str] = []
        checked = 0
        for claim in _sentences(_CITE_RE.sub("", answer_text)):
            ctoks = _tok(claim)
            if len(ctoks) < 2:
                continue
            checked += 1
            best = max((len(ctoks & st) / len(ctoks) for st in source_sets),
                       default=0.0)
            if best < self.support_threshold:
                unsupported.append(claim)
        return VerificationResult(supported=not unsupported,
                                  unsupported_claims=unsupported, checked=checked)


# ----------------------------------------------------------- structured output
def _extract_json(text: str) -> dict | None:
    """Extract the first balanced JSON object, respecting strings/escapes so
    braces inside string values don't terminate the scan early."""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                try:
                    obj = json.loads(text[start:i + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def generate_json(llm: LLMClient, prompt: str, required_keys: list[str],
                  role: str = "planner", retries: int = 2,
                  sensitivity: str | None = None) -> dict:
    if sensitivity is None:
        sensitivity = classify_sensitivity(prompt)   # don't leak secrets to cloud
    system = ("Respond with ONLY a single valid JSON object — no prose, no "
              "markdown code fences, no commentary.")
    last = ""
    for _ in range(retries + 1):
        out = llm.complete(prompt + "\nJSON:", system, role=role,
                           sensitivity=sensitivity)
        obj = _extract_json(out)
        if obj is not None and all(k in obj for k in required_keys):
            return obj
        last = out
    raise RuntimeError(
        f"model did not return JSON with keys {required_keys}: {last[:200]}")


class GroundedPlanner(Planner):
    """LLM planner that can only emit steps bound to registered tools.

    Unknown/hallucinated tool names are dropped; if nothing valid survives (or in
    offline/mock mode) it falls back to the deterministic heuristic planner.
    """

    def plan(self, goal: str) -> Plan:
        if self.llm._effective_mode() != "real":
            return super().plan(goal)
        try:
            tools = [self.registry.get(n) for n in self.registry.names()]
            catalog = "\n".join(
                f"- {t.name} ({t.risk.value}): {t.description}"
                for t in tools if t is not None)
            prompt = (
                f"Goal: {goal}\n\nAvailable tools (use ONLY these tool names):\n"
                f"{catalog}\n\nReturn JSON: "
                '{"steps": [{"intent": "...", "tool": "<one of the tool names>", '
                '"args": {}}]}')
            obj = generate_json(self.llm, prompt, ["steps"])
            steps: list[Step] = []
            for s in obj.get("steps", []):
                if not isinstance(s, dict):
                    continue
                tool = s.get("tool")
                if not isinstance(tool, str) or self.registry.get(tool) is None:
                    continue                      # drop hallucinated/unknown tools
                args = s.get("args")
                intent = str(s.get("intent", "step"))
                steps.append(Step(intent, tool,
                                  args if isinstance(args, dict) else {}))
            return Plan(goal=goal, steps=steps) if steps else super().plan(goal)
        except Exception:
            return super().plan(goal)             # safe fallback
