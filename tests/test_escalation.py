"""D6 adaptive cascade: cheap-first, escalate-on-low-confidence, budget-gated."""
from hybridagent.escalation import AdaptiveCascade
from hybridagent.grounding import ABSTAIN, GroundedResponder
from hybridagent.rag import RetrievedChunk
from hybridagent.router import HARD


# ------------------------------------------------------------------ primitive
def test_cheap_answer_accepted_no_escalation():
    calls = []

    def solve(diff):
        calls.append(diff)
        return "good"

    res = AdaptiveCascade().run(solve, accept=lambda a: a == "good")
    assert res.answer == "good" and res.escalated is False
    assert res.tier == "routed" and res.passes == 1
    assert calls == [None]                       # only the cheap pass ran


def test_low_confidence_triggers_escalation():
    def solve(diff):
        return "weak" if diff is None else "strong-answer"

    res = AdaptiveCascade().run(solve, accept=lambda a: a == "strong-answer")
    assert res.escalated is True and res.tier == "strong"
    assert res.answer == "strong-answer" and res.passes == 2
    assert res.reason == "escalated"


def test_escalation_uses_hard_tier():
    seen = []

    def solve(diff):
        seen.append(diff)
        return "no" if diff is None else "yes"

    AdaptiveCascade().run(solve, accept=lambda a: a == "yes")
    assert seen == [None, HARD]                  # the second pass forced HARD


def test_budget_blocks_escalation():
    calls = []

    def solve(diff):
        calls.append(diff)
        return "weak"

    res = AdaptiveCascade(can_escalate=lambda: False).run(
        solve, accept=lambda a: a == "good")
    assert res.escalated is False and res.reason == "budget"
    assert res.answer == "weak" and calls == [None]   # never escalated


def test_both_passes_rejected_keeps_strong():
    res = AdaptiveCascade().run(lambda diff: "bad", accept=lambda a: False)
    assert res.escalated is True and res.reason == "unverified"
    assert res.tier == "strong" and res.passes == 2


# ---------------------------------------------------------- grounding wiring
class _FakeLLM:
    """Scripted real-mode LLM: abstains at the routed tier, answers when forced HARD."""

    def __init__(self, routed: str, strong: str) -> None:
        self.routed, self.strong = routed, strong
        self.difficulties: list = []

    def _effective_mode(self) -> str:
        return "real"

    def complete(self, prompt, system=None, role="general",
                 sensitivity="normal", difficulty=None) -> str:
        self.difficulties.append(difficulty)
        return self.strong if difficulty else self.routed


def _src():
    return [RetrievedChunk(text="Paris is the capital of France.",
                           source="doc1", score=1.0, kind="kb", provenance="kb")]


def test_grounded_responder_escalates_on_abstain():
    llm = _FakeLLM(routed=f"{ABSTAIN} — not sure", strong="Paris. [S1]")
    ans = GroundedResponder(llm).answer("What is the capital of France?", _src())
    assert ans.escalated is True and ans.tier == "strong"
    assert ans.abstained is False and "Paris" in ans.text
    assert llm.difficulties == [None, HARD]


def test_grounded_responder_respects_budget_gate():
    llm = _FakeLLM(routed=f"{ABSTAIN} — not sure", strong="Paris. [S1]")
    ans = GroundedResponder(llm, can_escalate=lambda: False).answer(
        "What is the capital of France?", _src())
    assert ans.escalated is False and ans.abstained is True
    assert llm.difficulties == [None]            # escalation skipped
