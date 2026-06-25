from hybridagent import config as cfg
from hybridagent.persistence import Store
from hybridagent.skill_evaluator import SkillEvaluator
from hybridagent.skills import Skill, SkillLibrary


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_skill_outcomes_update_quality_score(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lib = SkillLibrary(store=Store.open())
    lib.add(Skill(name="followup", trigger="follow up email"))
    ev = SkillEvaluator(lib)
    ev.record("followup", "goal 1", "success")
    meta = ev.record("followup", "goal 2", "failure")
    assert meta["usage_count"] == 2
    assert meta["success_count"] == 1
    assert meta["failure_count"] == 1
    assert meta["quality_score"] == 0.5


def test_low_quality_skill_is_quarantined_and_not_retrieved(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lib = SkillLibrary(store=Store.open())
    lib.add(Skill(name="bad-skill", trigger="customer follow up email"))
    ev = SkillEvaluator(lib)
    for i in range(3):
        ev.record("bad-skill", f"goal {i}", "failure")
    assert ev.quarantine_low_quality(min_uses=3, threshold=0.4) == ["bad-skill"]
    assert lib.metadata("bad-skill")["quarantined"] == 1
    assert lib.retrieve("customer follow up email") == []


def test_unquarantine_restores_retrieval(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lib = SkillLibrary(store=Store.open())
    lib.add(Skill(name="ok-skill", trigger="customer follow up email"))
    lib.rag.store.set_skill_quarantine("ok-skill", True)
    assert lib.retrieve("customer follow up email") == []
    lib.unquarantine("ok-skill")
    assert lib.retrieve("customer follow up email")[0].name == "ok-skill"


def test_impact_report(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    lib = SkillLibrary(store=Store.open())
    ev = SkillEvaluator(lib)
    ev.record("skill-x", "goal", "success")
    report = ev.impact_report("skill-x")
    assert "uses=1" in report
    assert "quality=1.00" in report
