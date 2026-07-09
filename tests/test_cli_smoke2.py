"""Second CLI/tools coverage batch — core commands (handle, ask, onboard, tasks,
wiki, route explain) and real_tools honest-fail paths, all offline."""


from hybridagent import cli
from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_LLM", "mock")
    monkeypatch.setenv("PRAXIS_EMBED", "mock")
    monkeypatch.setenv("PRAXIS_WORK_DIR", str(tmp_path))


def _run(argv):
    try:
        return cli.main(argv)
    except SystemExit as e:
        return e.code


# ----------------------------------------------------------------- core commands
def test_cli_handle(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = _run(["handle", "draft a short status update"])
    assert rc == 0


def test_cli_onboard_noninteractive(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    assert _run(["onboard", "--provider", "ollama", "--model", "llama3.1"]) == 0
    out = capsys.readouterr().out.lower()
    assert "ollama" in out or "configured" in out
    # config persisted
    c = cfg.load_config()
    assert c.get("agents", {}).get("defaults", {}).get("model") == "ollama/llama3.1"


def test_cli_ask_empty_kb(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = _run(["ask", "what is in the knowledge base?"])
    assert rc in (0, 1)


def test_cli_tasks_lifecycle(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    _run(["task-create", "do something later"])
    assert _run(["tasks"]) == 0


def test_cli_route_explain(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    assert _run(["route"]) == 0
    out = capsys.readouterr().out.lower()
    assert "role" in out or "general" in out or "model" in out


def test_cli_eval(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = _run(["eval", "--category", "safety"])
    assert rc in (0, 1)


def test_cli_wiki_sources_empty(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    assert _run(["wiki-sources"]) == 0


def test_cli_unknown_command(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = _run(["this-is-not-a-command"])
    assert rc != 0  # argparse rejects


def test_cli_no_args_shows_help(tmp_path, monkeypatch, capsys):
    _isolate(tmp_path, monkeypatch)
    rc = _run([])
    # no subcommand -> help/usage, non-crashing
    assert rc in (0, 1, 2)


# ------------------------------------------------------------- real_tools paths
def test_generate_image_requires_prompt(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import generate_image
    assert "required" in generate_image(prompt="")


def test_generate_image_no_provider(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    from hybridagent.real_tools import generate_image
    assert "no image provider" in generate_image(prompt="a cat")


def test_tts_requires_text(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import text_to_speech
    assert "required" in text_to_speech(text="")


def test_tts_no_provider(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    from hybridagent.real_tools import text_to_speech
    assert "no TTS provider" in text_to_speech(text="hello")


def test_run_shell_empty(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import run_shell
    assert "required" in run_shell(command="")


def test_send_message_requires_args(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import send_message
    assert "required" in send_message(target="", text="")


def test_call_agent_requires_args(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import call_agent
    assert "required" in call_agent(target="", goal="")


def test_delegate_requires_goal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import delegate
    assert "required" in delegate(goal="")


def test_query_knowledge_empty_kb(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import query_knowledge
    out = query_knowledge(question="anything")
    assert "no indexed knowledge" in out or "knowledge" in out.lower()


def test_fetch_url_bad_scheme(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.real_tools import fetch_url
    out = fetch_url("file:///etc/passwd")
    assert "blocked" in out or "unsupported scheme" in out or "refusing" in out
