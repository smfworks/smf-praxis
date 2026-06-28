from hybridagent import PraxisAgent
from hybridagent import config as cfg
from hybridagent.grounding import GroundedPlanner, GroundedResponder, _extract_json, generate_json
from hybridagent.rag import RetrievedChunk
from hybridagent.tools import default_registry


class FakeLLM:
    """Minimal stand-in: forces 'real' mode and returns a fixed completion."""

    def __init__(self, out: str, mode: str = "real"):
        self.out = out
        self._mode = mode

    def _effective_mode(self):
        return self._mode

    def complete(self, prompt, system=None, role="general", sensitivity="normal", difficulty=None):
        return self.out


def _src(text, source="s.txt"):
    return RetrievedChunk(text=text, source=source, score=1.0)


def test_abstains_with_no_sources():
    ans = GroundedResponder().answer("anything?", [])
    assert ans.abstained and "INSUFFICIENT_EVIDENCE" in ans.text


def test_abstains_when_sources_irrelevant():
    srcs = [_src("A sourdough recipe with a long fermentation.", "bread.txt")]
    ans = GroundedResponder().answer("What is the AdventHealth revenue?", srcs)
    assert ans.abstained


def test_extractive_answer_cites_and_verifies():
    srcs = [_src("AdventHealth revenue grew 12 percent in Q3 on inpatient volume.",
                 "fin.txt")]
    ans = GroundedResponder().answer("How did AdventHealth revenue change in Q3?", srcs)
    assert not ans.abstained
    assert "[S1]" in ans.text
    assert "fin.txt" in ans.citations
    assert ans.verification is not None and ans.verification.supported


def test_verify_flags_unsupported_claim():
    srcs = [_src("The sky is blue today.")]
    v = GroundedResponder().verify(
        "The sky is blue today. The moon is made of cheese.", srcs)
    assert v.supported is False
    assert any("moon" in c for c in v.unsupported_claims)


def test_generate_json_parses_and_validates():
    obj = generate_json(FakeLLM('here you go: {"steps": [], "ok": true} done'),
                        "prompt", ["steps", "ok"])
    assert obj == {"steps": [], "ok": True}


def test_generate_json_raises_on_missing_keys():
    import pytest
    with pytest.raises(RuntimeError):
        generate_json(FakeLLM("no json here"), "prompt", ["steps"])


def test_extract_json_handles_braces_inside_strings():
    # Braces/quotes inside JSON string values must not terminate the scan early.
    assert _extract_json('{"a": "x}y", "b": 1}') == {"a": "x}y", "b": 1}
    assert _extract_json('prefix {"steps": []} suffix') == {"steps": []}
    assert _extract_json('{"o": {"k": "}"}, "n": 2}') == {"o": {"k": "}"}, "n": 2}
    assert _extract_json('{"s": "say \\"hi\\""}') == {"s": 'say "hi"'}
    assert _extract_json("no json here") is None


def test_generate_json_keeps_sensitive_prompt_off_cloud():
    captured = {}

    class LLM:
        def _effective_mode(self):
            return "real"

        def complete(self, prompt, system=None, role="general", sensitivity="normal", difficulty=None):
            captured["sensitivity"] = sensitivity
            return '{"ok": true}'

    generate_json(LLM(), "draft the reply containing SSN 123-45-6789", ["ok"])
    assert captured["sensitivity"] == "sensitive"


def test_grounded_planner_drops_hallucinated_tools():
    reg = default_registry()
    llm = FakeLLM('{"steps": ['
                  '{"intent": "read mail", "tool": "search_mail", "args": {}},'
                  '{"intent": "do evil", "tool": "rm_rf_everything", "args": {}}]}')
    plan = GroundedPlanner(reg, llm).plan("review mail")
    tools = [s.tool for s in plan.steps]
    assert "search_mail" in tools
    assert "rm_rf_everything" not in tools          # unknown tool dropped


def test_grounded_planner_falls_back_when_no_valid_steps():
    reg = default_registry()
    llm = FakeLLM('{"steps": [{"intent": "x", "tool": "nope", "args": {}}]}')
    plan = GroundedPlanner(reg, llm).plan("Prepare a customer follow-up email")
    # Fell back to the heuristic planner, which emits real registered tools.
    assert plan.steps and all(reg.get(s.tool) is not None for s in plan.steps)


def test_agent_ask_abstains_on_empty_kb(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    agent = PraxisAgent.persistent()
    ans = agent.ask("what is our Q3 revenue?")
    assert ans.abstained


def test_agent_ask_grounds_after_ingest(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    agent = PraxisAgent.persistent()
    agent.rag.ingest_text(
        "AdventHealth Q3 revenue grew 12 percent on inpatient volume.",
        source="fin.txt")
    ans = agent.ask("How did AdventHealth Q3 revenue change?")
    assert not ans.abstained
    assert ans.citations
