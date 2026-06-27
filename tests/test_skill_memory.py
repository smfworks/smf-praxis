from pathlib import Path

from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.skills import Skill, SkillLibrary

_NO_DISK = Path("__praxis_test_no_such_skills_dir__")


def _lib(*skills):
    lib = SkillLibrary(store=None, root=_NO_DISK)
    lib.skills = {s.name: s for s in skills}
    return lib


_EXPENSE = Skill(name="expense-report", trigger="filing an expense or reimbursement",
                 body="1. Gather receipts. 2. Draft the form. 3. Submit for approval.")
_FOLLOWUP = Skill(name="customer-followup", trigger="following up with a customer",
                  body="1. Review notes. 2. Draft a recap. 3. Send after approval.")


def test_bm25_retrieve_discriminates():
    lib = _lib(_EXPENSE, _FOLLOWUP)
    top = lib.retrieve("file an expense reimbursement for travel", k=1)
    assert top and top[0].name == "expense-report"
    top2 = lib.retrieve("follow up with the customer after the meeting", k=1)
    assert top2 and top2[0].name == "customer-followup"


def test_recall_context_formats_relevant_skill():
    lib = _lib(_EXPENSE, _FOLLOWUP)
    ctx = lib.recall_context("I need to file an expense reimbursement", k=1)
    assert "Relevant learned procedures" in ctx
    assert "expense-report" in ctx and "Gather receipts" in ctx


def test_recall_context_empty_when_no_skills():
    assert _lib().recall_context("anything") == ""


def test_disabled_skill_not_retrieved():
    disabled = Skill(name="expense-report",
                     trigger="filing an expense or reimbursement",
                     body="...", enabled=False)
    lib = _lib(disabled, _FOLLOWUP)
    names = [s.name for s in lib.retrieve("file an expense reimbursement", k=5)]
    assert "expense-report" not in names


def test_recall_context_respects_char_budget():
    big = Skill(name="big-skill", trigger="x",
                body="step. " * 400)  # ~2400 chars, over the default budget
    lib = _lib(big)
    ctx = lib.recall_context("x", k=1, max_chars=200)
    assert ctx == ""  # the only hit exceeds the budget, so nothing is injected


def test_daemon_grounds_agent_turn_with_skills(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.delenv("PRAXIS_SKILL_RECALL", raising=False)
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    assert d.agent.skills is not None
    d.agent.skills.skills["expense-report"] = _EXPENSE
    out = d._ground_with_skills(
        "BASE", [{"role": "user", "content": "help me file an expense reimbursement"}])
    assert "BASE" in out and "expense-report" in out


def test_daemon_skill_recall_can_be_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_SKILL_RECALL", "0")
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    d._ensure_agent()
    d.agent.skills.skills["expense-report"] = _EXPENSE
    out = d._ground_with_skills(
        "BASE", [{"role": "user", "content": "file an expense reimbursement"}])
    assert out == "BASE"  # firewall off -> unchanged
