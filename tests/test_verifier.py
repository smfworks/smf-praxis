from hybridagent import config as cfg
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.chat_agent import GovernedChatAgent
from hybridagent.tools import Tool, ToolRegistry
from hybridagent.verifier import (
    AnswerVerifier,
    VerificationConfig,
    VerifiedChatAgent,
)


def _schema(*required):
    return {"type": "object",
            "properties": {k: {"type": "string"} for k in required},
            "required": list(required)}


def _registry(*tools):
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)
    return reg


_SEND = Tool("send_email", RiskClass.SEND, "Send",
             lambda draft_id="", **k: f"SENT {draft_id}", parameters=_schema("draft_id"))
_ECHO = Tool("echo", RiskClass.DRAFT, "Echo", lambda message="", **k: f"echo:{message}",
             parameters=_schema("message"))


def _types(events):
    return [e.type for e in events]


def _final(events):
    return next((e for e in reversed(events) if e.type == "final"), None)


# ----------------------------------------------------------- AnswerVerifier unit
def test_verify_flags_false_completion_when_held():
    v = AnswerVerifier()
    verdict = v.verify("send it", "I've sent the email.", held=True)
    assert not verdict.approved and "action_claim_consistency" in verdict.checks


def test_verify_allows_honest_pending_answer_when_held():
    v = AnswerVerifier()
    verdict = v.verify("send it", "It's drafted and pending your approval.", held=True)
    assert verdict.approved


def test_verify_flags_false_claim_when_denied():
    v = AnswerVerifier()
    verdict = v.verify("delete it", "Done, I deleted the file.", action_denied=True)
    assert not verdict.approved and "action_claim_consistency" in verdict.checks


def test_verify_flags_empty_answer():
    v = AnswerVerifier()
    assert not v.verify("hello", "").approved
    assert not v.verify("hello", "(no response)").approved


def test_verify_clean_answer_approved_without_critic():
    v = AnswerVerifier()
    assert v.verify("2+2?", "It is 4.").approved


def test_verify_uses_optional_critic():
    revise = AnswerVerifier(critic=lambda task, ans: "REVISE: missing the unit")
    approve = AnswerVerifier(critic=lambda task, ans: "APPROVE")
    assert not revise.verify("q", "an answer").approved
    assert approve.verify("q", "an answer").approved


# ----------------------------------------------------------- wrapper behaviour
_DELETE = Tool("delete_account", RiskClass.DESTRUCTIVE, "Delete",
               lambda id="", **k: "DELETED", parameters=_schema("id"))


class _OverclaimsThenHonest:
    """Claims a DENIED delete succeeded, then corrects once a reviewer rejects."""

    def __init__(self):
        self.calls = 0

    def chat_tools(self, messages, tools=None, system=None):
        self.calls += 1
        if system and "reviewer rejected" in system.lower():
            return {"text": "I could not delete the account; it was blocked.",
                    "tool_calls": []}
        if any(m.get("role") == "tool" for m in messages):
            return {"text": "Done — I deleted the account.", "tool_calls": []}
        return {"text": "", "tool_calls": [
            {"id": "c1", "name": "delete_account", "args": {"id": "a1"}}]}


def _run(llm, registry, broker, *, max_revisions=1):
    inner = GovernedChatAgent(llm, registry, broker)
    return list(VerifiedChatAgent(inner, max_revisions=max_revisions).run(
        [{"role": "user", "content": "handle the request"}]))


def test_wrapper_catches_and_revises_denied_false_claim():
    # delete_account is not allowlisted -> DENIED (nothing queued or executed),
    # so the dishonest "I deleted it" answer is safely revised.
    llm = _OverclaimsThenHonest()
    broker = GovernanceBroker(GovernancePolicy(allowed_tools=set()))
    events = _run(llm, _registry(_DELETE), broker)
    types = _types(events)
    assert "denied" in types and "verification" in types and "approval" not in types
    text = _final(events).data["text"]
    assert "could not delete" in text and "Done" not in text
    assert llm.calls == 3


def test_wrapper_flags_held_false_claim_without_retrying():
    # A HELD send falsely reported as done must be flagged but NEVER re-run:
    # re-running would queue a second approval and risk double execution.
    class _ClaimsHeldSent:
        def __init__(self):
            self.calls = 0

        def chat_tools(self, messages, tools=None, system=None):
            self.calls += 1
            return {"text": "I've sent the email.", "tool_calls": [
                {"id": "c1", "name": "send_email", "args": {"draft_id": "d1"}}]}

    llm = _ClaimsHeldSent()
    broker = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    events = _run(llm, _registry(_SEND), broker)
    types = _types(events)
    assert "verification" in types  # flagged
    assert len([e for e in events if e.type == "approval"]) == 1  # no duplicate
    assert llm.calls == 1  # not retried


def test_wrapper_no_verification_event_on_honest_answer():
    class _Honest:
        def chat_tools(self, messages, tools=None, system=None):
            return {"text": "Here is the summary you asked for.", "tool_calls": []}

    events = _run(_Honest(), _registry(_SEND),
                  GovernanceBroker(GovernancePolicy()))
    assert "verification" not in _types(events)
    assert _final(events).data["text"].startswith("Here is the summary")


def test_wrapper_no_retry_after_side_effect_but_surfaces_finding():
    # A draft executes (real side effect) AND a send is held; the answer falsely
    # claims completion. The finding is surfaced but the turn is NOT re-run.
    class _DraftAndHoldThenClaim:
        def __init__(self):
            self.calls = 0

        def chat_tools(self, messages, tools=None, system=None):
            self.calls += 1
            return {"text": "I've sent it.", "tool_calls": [
                {"id": "c1", "name": "echo", "args": {"message": "hi"}},
                {"id": "c2", "name": "send_email", "args": {"draft_id": "d1"}}]}

    llm = _DraftAndHoldThenClaim()
    broker = GovernanceBroker(
        GovernancePolicy(allowed_tools={"echo", "send_email"}))
    events = _run(llm, _registry(_ECHO, _SEND), broker)
    types = _types(events)
    assert "tool_result" in types  # the draft executed (side effect)
    assert "verification" in types  # finding surfaced
    assert llm.calls == 1  # but NOT retried


def test_wrapper_bounded_revisions():
    class _AlwaysEvasive:
        def __init__(self):
            self.calls = 0

        def chat_tools(self, messages, tools=None, system=None):
            self.calls += 1
            return {"text": "", "tool_calls": []}  # empty -> non-evasive failure

    llm = _AlwaysEvasive()
    events = _run(llm, _registry(_SEND), GovernanceBroker(GovernancePolicy()),
                  max_revisions=1)
    assert "verification" in _types(events)
    assert llm.calls == 2  # one revision, then accept


def test_config_defaults_and_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_VERIFY", raising=False)
    assert VerificationConfig.load().enabled is True
    monkeypatch.setenv("PRAXIS_VERIFY", "0")
    assert VerificationConfig.load().enabled is False


# ---------------------------------------------------------------------------
# H05 — optional LLM-verifier critic backend (arXiv:2607.05391).
# These tests cover the parts that work WITHOUT the optional llm-verifier
# package installed: config parsing, the deterministic-only fallback, and
# the MissingVerifierBackendError path. The live-backend integration test is
# marked optional and skips when the library or a backend is absent, so the
# core test suite stays green in the dependency-free env.
# ---------------------------------------------------------------------------

def test_llm_verifier_config_disabled_by_default():
    """Without `critic: "llm-verifier"` in praxis.json, no critic is built
    and the deterministic path is unchanged."""
    from hybridagent.verifier_llm import LLMVerifierConfig
    c = LLMVerifierConfig.from_verification_dict({})
    assert c.enabled is False
    c2 = LLMVerifierConfig.from_verification_dict({"critic": "deterministic"})
    assert c2.enabled is False


def test_llm_verifier_config_opts_in():
    """`critic: "llm-verifier"` opts in; fields are parsed and clamped."""
    from hybridagent.verifier_llm import LLMVerifierConfig
    v = {"critic": "llm-verifier",
         "nEvaluations": "16",
         "threshold": "0.7",
         "model": "qwen3.5-9b",
         "criteria": {"Spec": "satisfies spec?"}}
    c = LLMVerifierConfig.from_verification_dict(v)
    assert c.enabled is True
    assert c.n_evaluations == 16
    assert c.threshold == 0.7
    assert c.model == "qwen3.5-9b"
    assert c.criteria == {"Spec": "satisfies spec?"}


def test_llm_verifier_config_clamps_out_of_range():
    from hybridagent.verifier_llm import LLMVerifierConfig
    c = LLMVerifierConfig.from_verification_dict(
        {"critic": "llm-verifier", "nEvaluations": 999, "threshold": -1.0})
    assert c.n_evaluations == 64      # clamped to max
    assert c.threshold == 0.0         # clamped to [0, 1]


def test_verification_config_load_falls_back_when_library_missing(monkeypatch, tmp_path):
    """When the operator opts in but llm-verifier is not installed,
    VerificationConfig.load must fall back to deterministic-only (critic=None)
    rather than break the chat loop. Simulates the library being absent even
    when it happens to be installed in the dev env."""
    import sys
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_VERIFY", raising=False)
    import json
    home = tmp_path / ".praxis"
    home.mkdir()
    (home / "praxis.json").write_text(json.dumps({
        "agents": {"verification": {"critic": "llm-verifier"}}}))
    # Force ImportError on `import llm_verifier` so build_llm_verifier_critic
    # raises MissingVerifierBackendError, which load() catches -> critic=None.
    real = sys.modules.get("llm_verifier")
    sys.modules["llm_verifier"] = None
    try:
        vc = VerificationConfig.load()
    finally:
        if real is not None:
            sys.modules["llm_verifier"] = real
        else:
            sys.modules.pop("llm_verifier", None)
    assert vc.enabled is True
    assert vc.critic is None  # fell back to deterministic-only


def test_llm_verifier_critic_raises_when_library_missing():
    """Constructing LLMVerifierCritic without the library raises
    MissingVerifierBackendError — the operator sees the install hint."""
    import importlib
    import sys

    from hybridagent.verifier_llm import (
        LLMVerifierConfig,
        LLMVerifierCritic,
        MissingVerifierBackendError,
    )
    # Ensure the library appears not-installed for this test, regardless of
    # whether it happens to be installed in the env.
    real_llm_verifier = sys.modules.get("llm_verifier")
    sys.modules["llm_verifier"] = None  # force ImportError on import
    try:
        config = LLMVerifierConfig(enabled=True)
        try:
            LLMVerifierCritic(config)
            raise AssertionError("expected MissingVerifierBackendError")
        except MissingVerifierBackendError:
            pass  # correct — operator-facing install hint
    finally:
        # Restore the real module so later tests aren't poisoned.
        if real_llm_verifier is not None:
            sys.modules["llm_verifier"] = real_llm_verifier
        else:
            sys.modules.pop("llm_verifier", None)
            importlib.invalidate_caches()


# ---------------------------------------------------------------------------
# Live-backend integration test — skips when llm-verifier or a backend is
# absent. Run manually with a configured VERTEX_API_KEY or OPENAI_BASE_URL.
# ---------------------------------------------------------------------------

def test_llm_verifier_critic_live_backend():
    pytest = __import__("pytest")
    try:
        import llm_verifier  # noqa: F401
    except ImportError:
        pytest.skip("llm-verifier not installed (optional extra)")
    import os
    if not (os.environ.get("VERTEX_API_KEY")
            or os.environ.get("OPENAI_BASE_URL")):
        pytest.skip("no logprob-exposing backend configured "
                    "(set VERTEX_API_KEY or OPENAI_BASE_URL)")
    from hybridagent.verifier_llm import LLMVerifierConfig, LLMVerifierCritic
    config = LLMVerifierConfig(enabled=True, n_evaluations=2, threshold=0.3)
    critic = LLMVerifierCritic(config)
    verdict = critic("What is 2+2? Reply with just the number.",
                     "4")
    assert verdict.startswith("APPROVE") or verdict.startswith("REVISE:")
