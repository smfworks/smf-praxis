"""End-to-end CLI dispatch tests.

These drive ``hybridagent.cli.main([...])`` directly with an isolated
``PRAXIS_HOME`` and offline mock mode so the whole command surface is exercised
without network or real models. Raises cli.py coverage off 0%.
"""
import wave

import pytest

from hybridagent import cli
from hybridagent import config as cfg


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    monkeypatch.setenv("PRAXIS_LLM", "mock")
    monkeypatch.setenv("PRAXIS_EMBED", "mock")
    monkeypatch.setenv("PRAXIS_MM", "mock")
    return tmp_path


def _run(argv):
    return cli.main(argv)


def test_handle_command(capsys):
    assert _run(["handle", "Review recent mail and save a brief"]) == 0
    out = capsys.readouterr().out
    assert "goal:" in out
    assert "[read]" in out


def test_handle_with_approve_all(capsys):
    assert _run(["handle", "Prepare a customer follow-up email", "--approve-all"]) == 0
    out = capsys.readouterr().out
    assert "auto-approving" in out


def test_heartbeat_command(capsys):
    assert _run(["heartbeat", "--watch", "scan for follow-ups"]) == 0
    assert "goal:" in capsys.readouterr().out


def test_remember_command(capsys):
    assert _run(["remember", "Michael prefers concise briefs",
                 "--kind", "preference"]) == 0
    assert "stored durable preference" in capsys.readouterr().out


def test_approvals_and_approve_flow(capsys):
    _run(["handle", "Prepare a customer follow-up email"])
    capsys.readouterr()
    assert _run(["approvals"]) == 0
    out = capsys.readouterr().out
    assert "pending approval" in out
    import re
    aid = re.search(r"appr-[0-9a-f]{8}", out).group(0)
    assert _run(["approve", aid, "--approved-by", "michael",
                 "--notes", "ok"]) == 0
    assert "SENT" in capsys.readouterr().out


def test_compliance_command(capsys):
    _run(["handle", "Review recent mail and save a brief"])
    capsys.readouterr()
    assert _run(["compliance"]) == 0
    assert "PASS" in capsys.readouterr().out


def test_task_lifecycle(capsys):
    assert _run(["task-create", "Review recent mail and save a brief"]) == 0
    out = capsys.readouterr().out
    import re
    tid = re.search(r"task-[0-9a-f]{10}", out).group(0)
    assert _run(["tasks"]) == 0
    assert tid in capsys.readouterr().out
    assert _run(["task-run", tid]) == 0
    assert "completed" in capsys.readouterr().out
    assert _run(["task-cancel", tid]) == 0


def test_ingest_recall_ask(capsys, tmp_path):
    doc = tmp_path / "fin.md"
    doc.write_text("AdventHealth Q3 revenue grew 12 percent on inpatient volume.",
                   encoding="utf-8")
    assert _run(["ingest", str(doc)]) == 0
    assert "chunks" in capsys.readouterr().out
    assert _run(["recall", "AdventHealth revenue"]) == 0
    assert "fin.md" in capsys.readouterr().out
    assert _run(["ask", "How did AdventHealth Q3 revenue change?"]) == 0
    assert "fin.md" in capsys.readouterr().out


def test_ask_abstains_on_empty_kb(capsys):
    assert _run(["ask", "what is the Q3 revenue?"]) == 0
    assert "INSUFFICIENT EVIDENCE" in capsys.readouterr().out


def test_route_command(capsys):
    assert _run(["route"]) == 0
    out = capsys.readouterr().out
    assert "sensitivity" in out and "planner" in out


def test_describe_text(capsys, tmp_path):
    doc = tmp_path / "n.txt"
    doc.write_text("project goals and milestones", encoding="utf-8")
    assert _run(["describe", str(doc)]) == 0
    assert "milestones" in capsys.readouterr().out


def test_describe_audio_metadata(capsys, tmp_path):
    wav = tmp_path / "memo.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(b"\x00\x00" * 8000)
    assert _run(["describe", str(wav)]) == 0
    assert "Offline mock" in capsys.readouterr().out


def test_learn_skills_skill_flow(capsys):
    assert _run(["learn", "Prepare a customer follow-up", "--name",
                 "followup", "--yes"]) == 0
    assert "saved skill" in capsys.readouterr().out
    assert _run(["skills"]) == 0
    assert "followup" in capsys.readouterr().out
    assert _run(["skill", "followup"]) == 0
    assert "followup" in capsys.readouterr().out


def test_skill_record_and_evaluate(capsys):
    _run(["learn", "customer follow up email", "--name", "fu", "--yes"])
    capsys.readouterr()
    for _ in range(3):
        _run(["skill-record", "fu", "g", "failure"])
    capsys.readouterr()
    assert _run(["skill-evaluate", "--min-uses", "3", "--threshold", "0.4"]) == 0
    assert "quarantined" in capsys.readouterr().out


def test_subagent_run_and_list(capsys):
    assert _run(["subagent-run", "research recent mail", "--role",
                 "researcher"]) == 0
    assert "completed" in capsys.readouterr().out
    assert _run(["subagents"]) == 0
    assert "agent-researcher" in capsys.readouterr().out


def test_health_command(capsys):
    assert _run(["health"]) == 0
    assert "status:" in capsys.readouterr().out


def test_wiki_commands(capsys, tmp_path):
    doc = tmp_path / "policy.md"
    doc.write_text("Clinical audit policy is mandatory.", encoding="utf-8")
    assert _run(["wiki-add", str(doc), "--refresh-hours", "0"]) == 0
    out = capsys.readouterr().out
    assert "registered" in out
    assert _run(["wiki-sources"]) == 0
    assert "kb" in capsys.readouterr().out
    assert _run(["wiki-refresh"]) == 0
    assert "refreshed" in capsys.readouterr().out


def test_wiki_add_refuses_file_uri(capsys):
    assert _run(["wiki-add", "file:///etc/passwd"]) == 1
    assert "refused" in capsys.readouterr().out


def test_scratchpad_write_read(capsys):
    assert _run(["scratchpad-write", "k1", "shared note",
                 "--written-by", "agent-researcher"]) == 0
    assert _run(["scratchpad-read", "k1"]) == 0
    assert "shared note" in capsys.readouterr().out


def test_memory_purge_command(capsys):
    _run(["remember", "from alice", "--kind", "fact"])
    capsys.readouterr()
    assert _run(["memory-purge", "--forget-provenance", "cli"]) == 0
    assert "removed" in capsys.readouterr().out


def test_unknown_command_errors():
    with pytest.raises(SystemExit):
        _run(["definitely-not-a-command"])
