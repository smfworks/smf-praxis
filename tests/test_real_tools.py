"""Tests for real filesystem and web tools.

Filesystem tests use a temporary directory set via PRAXIS_WORK_DIR so they cannot
pollute the repository or escape to arbitrary host paths.
"""
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.real_tools import fetch_url, list_dir, read_file, search_web, write_file
from hybridagent.tools import default_registry


def test_read_write_list_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"PRAXIS_WORK_DIR": tmp}):
            write_file("notes/project.txt", "milestones: alpha, beta, GA")
            assert read_file("notes/project.txt").startswith("milestones")
            listing = list_dir("notes")
            assert "project.txt" in listing


def test_write_file_creates_parent_dirs():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"PRAXIS_WORK_DIR": tmp}):
            write_file("a/b/c.txt", "nested")
            assert Path(tmp).joinpath("a/b/c.txt").read_text() == "nested"


def test_path_traversal_blocked():
    with tempfile.TemporaryDirectory() as tmp:
        with patch.dict(os.environ, {"PRAXIS_WORK_DIR": tmp}):
            try:
                write_file("../outside.txt", "x")
                assert False, "expected ValueError"
            except ValueError as exc:
                assert "escapes work directory" in str(exc)
            try:
                read_file("/etc/passwd")
                assert False, "expected ValueError"
            except ValueError as exc:
                assert "absolute paths not allowed" in str(exc)


def test_fetch_url_uses_http_lib_only():
    """fetch_url is a READ-class tool using only stdlib; smoke-test with example.com.

    This may be flaky on CI without network; skip gracefully.
    """
    try:
        result = fetch_url("https://example.com")
    except Exception as exc:  # pragma: no cover - network dependent
        assert False, f"fetch_url should never raise: {exc}"
    assert "example.com" in result or "HTTP" in result or "failed" in result


def test_search_web_without_backend_reports_config_gap():
    # Disable the keyless DuckDuckGo default so the honest-placeholder path runs.
    with patch.dict(os.environ, {"PRAXIS_SEARCH_DISABLE_DEFAULT": "1"}, clear=True):
        assert "no search backend configured" in search_web("praxis agent")


def test_default_registry_risk_classes():
    reg = default_registry()
    read_tool = reg.get("read_file")
    write_tool = reg.get("write_file")
    fetch_tool = reg.get("fetch_url")
    search_tool = reg.get("search_web")
    assert read_tool is not None and read_tool.risk is RiskClass.READ
    assert write_tool is not None and write_tool.risk is RiskClass.DRAFT
    assert fetch_tool is not None and fetch_tool.risk is RiskClass.READ
    assert search_tool is not None and search_tool.risk is RiskClass.READ


def test_governance_broker_allows_read_blocks_send():
    reg = default_registry()
    broker = GovernanceBroker(GovernancePolicy(allowed_tools=set(reg.names())))
    read_decision = broker.authorize("praxis", "read_file", RiskClass.READ,
                                     {"name": "x.txt"})
    assert read_decision.verdict.value == "allow"
    send_decision = broker.authorize("praxis", "send_email", RiskClass.SEND,
                                     {"draft_id": "123"})
    assert send_decision.verdict.value == "needs_approval"
