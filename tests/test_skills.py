from hybridagent import PraxisAgent
from hybridagent import config as cfg
from hybridagent.embeddings import EmbeddingClient
from hybridagent.persistence import Store
from hybridagent.skills import Skill, SkillLibrary, distill_skill


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_skill_markdown_roundtrip():
    sk = Skill(name="customer-followup",
               trigger="prepare and send a customer follow-up",
               body="Steps:\n1. perceive\n2. draft\n3. approve send",
               tags=["email", "customer"])
    text = sk.to_markdown()
    again = Skill.from_markdown(text)
    assert again.name == "customer-followup"
    assert again.trigger == sk.trigger
    assert again.tags == ["email", "customer"]
    assert again.enabled is True and again.version == 1


def test_library_add_persists_to_disk(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    lib = SkillLibrary(store=Store.open())
    lib.add(Skill(name="brief-prep", trigger="prepare a private brief",
                  body="1. gather\n2. summarize"))
    # SKILL.md written, and a fresh library rehydrates it from disk.
    assert lib.path_for(lib.get("brief-prep")).exists()
    lib2 = SkillLibrary(store=Store.open())
    assert "brief-prep" in [s.name for s in lib2.list()]


def test_skill_reload_does_not_reembed(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)

    class Counting(EmbeddingClient):
        calls = 0

        def embed(self, texts):
            Counting.calls += len(texts)
            return super().embed(texts)

    store = Store.open()
    lib = SkillLibrary(store=store, embedder=Counting(mode="mock"))
    lib.add(Skill(name="s1", trigger="follow up email"))
    base = Counting.calls
    SkillLibrary(store=store, embedder=Counting(mode="mock"))   # reload
    assert Counting.calls == base       # no re-embedding existing skills on load


def test_out_of_band_skill_edit_is_reindexed(tmp_path, monkeypatch):
    import os
    import time
    _home(tmp_path, monkeypatch)
    store = Store.open()
    lib = SkillLibrary(store=store)
    lib.add(Skill(name="s1", trigger="alpha original trigger words"))
    # Edit the SKILL.md directly on disk with new, distinctive content.
    path = lib.path_for(lib.get("s1"))
    path.write_text(Skill(name="s1",
                          trigger="beta replaced keyword zulu yankee").to_markdown(),
                    encoding="utf-8")
    future = time.time() + 10
    os.utime(path, (future, future))                # ensure mtime is newer
    lib2 = SkillLibrary(store=store)                 # reload => should re-index
    hits = lib2.retrieve("beta replaced keyword zulu yankee")
    assert hits and hits[0].name == "s1"            # matches NEW content, not stale


def test_library_semantic_retrieve(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    lib = SkillLibrary(store=Store.open())
    lib.add(Skill(name="followup-email",
                  trigger="prepare and send a customer follow-up email"))
    lib.add(Skill(name="expense-report",
                  trigger="file a monthly expense report with receipts"))
    hits = lib.retrieve("draft a follow-up email to the customer", k=1)
    assert hits and hits[0].name == "followup-email"


def test_distill_template_is_offline_and_deterministic():
    class MockLLM:
        def _effective_mode(self):
            return "mock"
    sk = distill_skill(MockLLM(), "Prepare a customer follow-up email",
                       ["draft follow-up -> create_email_draft"])
    assert sk.name and sk.trigger
    assert "create_email_draft" in sk.body


def test_agent_learn_skill_has_no_side_effects(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    draft = agent.learn_skill("Prepare a customer follow-up email")
    assert isinstance(draft, Skill)
    # Drafting must not save the skill or queue approvals (governed).
    assert agent.skills.list() == []
    assert agent.broker.pending == {}


def test_saved_skill_surfaces_in_perception(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    agent.skills.add(Skill(name="followup-flow",
                           trigger="customer follow-up email after a sync"))
    signals = agent.perception.sense(
        "draft a customer follow-up email", ["search_mail"])
    assert any(s.source == "skill:followup-flow" for s in signals)


def test_disabled_skill_is_not_surfaced(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    agent = PraxisAgent.persistent()
    agent.skills.add(Skill(name="off-skill",
                           trigger="customer follow-up email", enabled=False))
    signals = agent.perception.sense("customer follow-up email", ["search_mail"])
    assert not any(s.source == "skill:off-skill" for s in signals)
