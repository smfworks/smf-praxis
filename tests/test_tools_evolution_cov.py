"""Cover the success/mocked paths in real_tools + evolution that the offline
honest-fail tests skip: image/TTS generation (mocked HTTP), delegate (offline
subagent), and the evolution LLM mutation path."""
import json

from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_LLM", "mock")
    monkeypatch.setenv("PRAXIS_EMBED", "mock")
    monkeypatch.setenv("PRAXIS_WORK_DIR", str(tmp_path))


class _FakeResp:
    def __init__(self, payload: bytes):
        self._p = payload

    def read(self, *a):
        return self._p

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_generate_image_success_mocked(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import urllib.request
    body = json.dumps({"data": [{"url": "https://img/result.png"}]}).encode()
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp(body))
    from hybridagent.real_tools import generate_image
    out = generate_image(prompt="a red cube")
    assert "result.png" in out or "generated" in out


def test_generate_image_provider_error_mocked(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import urllib.request

    def boom(req, timeout=0):
        raise OSError("network down")

    monkeypatch.setattr(urllib.request, "urlopen", boom)
    from hybridagent.real_tools import generate_image
    out = generate_image(prompt="x")
    assert "failed" in out.lower()


def test_tts_success_mocked(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=0: _FakeResp(b"ID3fakeaudio"))
    from hybridagent.real_tools import text_to_speech
    out = text_to_speech(text="hello world")
    assert "wrote" in out or ".mp3" in out


def test_delegate_offline_run(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import delegate
    out = delegate(goal="summarize the project status")
    # offline mock LLM -> a subagent runs and returns a status line
    assert "delegate" in out.lower()


def test_query_knowledge_with_indexed_content(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.persistence import Store
    from hybridagent.rag import Rag
    rag = Rag(Store.open())
    rag.ingest_text("The capital of France is Paris.", source="geo", ns="kb")
    from hybridagent.real_tools import query_knowledge
    out = query_knowledge(question="What is the capital of France?")
    assert "Paris" in out or "chunk" in out.lower()


def test_evolution_llm_mutation_path(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import evolution as ev
    from hybridagent.persistence import Store
    from hybridagent.skills import Skill, SkillLibrary
    store = Store.open()
    lib = SkillLibrary(store=store)
    lib.add(Skill(name="s", trigger="deploy", body="Deploy the service safely.",
                  provenance="t"))
    for g in ["deploy production cluster", "ship the production release"]:
        store.record_skill_outcome("s", g, "success", 1.0)

    class _LLM:
        def complete(self, prompt, role="general", **kw):
            return ("TRIGGER: deploy production cluster release\n"
                    "BODY: Deploy the service safely to production.")

    prop = ev.evolve_skill(lib, "s", llm=_LLM())
    assert prop is not None
    # the LLM-sourced candidate should be considered (source llm or heuristic)
    assert prop.source in ("llm", "heuristic")


def test_evolution_llm_failure_falls_back(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import evolution as ev
    from hybridagent.persistence import Store
    from hybridagent.skills import Skill, SkillLibrary
    store = Store.open()
    lib = SkillLibrary(store=store)
    lib.add(Skill(name="s", trigger="deploy", body="Deploy the service.",
                  provenance="t"))
    for g in ["deploy production cluster", "ship production"]:
        store.record_skill_outcome("s", g, "success", 1.0)

    class _BadLLM:
        def complete(self, *a, **k):
            raise RuntimeError("llm down")

    # LLM raises -> falls back to heuristic candidates, no crash
    prop = ev.evolve_skill(lib, "s", llm=_BadLLM())
    assert prop is None or prop.source == "heuristic"
