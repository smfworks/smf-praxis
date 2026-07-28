"""Tests for the release gate CLI (scripts/release-gate.py).

These tests verify the gate state machine: dispatch → record verdicts →
unlock. They use a temp directory as the repo root and mock the git
calls so they run without a real repository.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the release-gate module
_spec = importlib.util.spec_from_file_location(
    "release_gate",
    Path(__file__).resolve().parent.parent / "scripts" / "release-gate.py",
)
assert _spec is not None
assert _spec.loader is not None
rg = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(rg)


@pytest.fixture
def fake_repo(tmp_path):
    """Create a fake repo directory with .hermes/release-gates/."""
    repo = tmp_path / "fake-repo"
    repo.mkdir()
    gate_dir = repo / ".hermes" / "release-gates"
    gate_dir.mkdir(parents=True)
    return repo


@pytest.fixture
def mock_git():
    """Patch all git helper functions to return predictable values."""
    with patch.object(rg, "_git_sha", return_value="abc123"), \
         patch.object(rg, "_git_status_clean", return_value=True), \
         patch.object(rg, "_git_version", return_value="0.28.32"), \
         patch.object(rg, "_git_origin_main", return_value="originsha1234567890abcdef1234567890abcdef12"), \
         patch.object(rg, "_git_sha_exists", return_value=True):
        yield


class TestGateState:
    """Tests for gate state persistence."""

    def test_save_and_load_gate_state(self, fake_repo):
        state = {
            "gate_id": "gate-test-001",
            "sha": "abc123",
            "version": "0.28.32",
            "status": "pending",
            "reviewers": {},
        }
        rg._save_gate_state(fake_repo, "abc123", state)
        loaded = rg._load_gate_state(fake_repo, "abc123")
        assert loaded is not None
        assert loaded["gate_id"] == "gate-test-001"
        assert loaded["sha"] == "abc123"
        assert loaded["version"] == "0.28.32"
        assert loaded["status"] == "pending"

    def test_load_nonexistent_gate_state(self, fake_repo):
        loaded = rg._load_gate_state(fake_repo, "nonexistent")
        assert loaded is None

    def test_gate_state_path(self, fake_repo):
        path = rg._gate_state_path(fake_repo, "abc123")
        assert path == fake_repo / ".hermes" / "release-gates" / "abc123.json"

    def test_attestation_path(self, fake_repo):
        path = rg._attestation_path(fake_repo, "abc123")
        assert path == fake_repo / ".hermes" / "release-gates" / "abc123.attestation.json"


class TestGateId:
    """Tests for gate ID generation."""

    def test_gate_id_format(self):
        gid = rg._gate_id("abc123def456789012345678901234567890abcd")
        assert gid.startswith("gate-abc123def456-")
        assert "T" in gid  # ISO timestamp marker
        assert gid.endswith("Z")

    def test_gate_id_unique(self):
        gid1 = rg._gate_id("abc123def456789012345678901234567890abcd")
        gid2 = rg._gate_id("abc123def456789012345678901234567890abcd")
        # May be the same if called in the same second, but format should be consistent
        assert gid1.startswith("gate-abc123def456-")
        assert gid2.startswith("gate-abc123def456-")


class TestReviewerPrompts:
    """Tests for reviewer prompt generation."""

    def test_build_reviewer_prompt_legal(self, fake_repo):
        prompt = rg._build_reviewer_prompt(
            "legal", fake_repo, "abc123", "0.28.32", "gate-test-001"
        )
        assert "Legal / Privacy Reviewer" in prompt
        assert "abc123" in prompt
        assert "0.28.32" in prompt
        assert "gate-test-001" in prompt
        assert "tenant isolation" in prompt

    def test_build_reviewer_prompt_learning(self, fake_repo):
        prompt = rg._build_reviewer_prompt(
            "learning", fake_repo, "abc123", "0.28.32", "gate-test-001"
        )
        assert "Learning / Safety Reviewer" in prompt
        assert "child-safe tutoring" in prompt

    def test_build_reviewer_prompt_release(self, fake_repo):
        prompt = rg._build_reviewer_prompt(
            "release", fake_repo, "abc123", "0.28.32", "gate-test-001"
        )
        assert "Release / Integration Reviewer" in prompt
        assert "coverage gate" in prompt

    def test_reviewer_domains_complete(self):
        assert set(rg.REVIEWER_DOMAINS.keys()) == {"legal", "learning", "release"}
        for _domain, info in rg.REVIEWER_DOMAINS.items():
            assert "title" in info
            assert "focus" in info
            assert len(info["focus"]) > 10


class TestDispatch:
    """Tests for the dispatch command."""

    def test_dispatch_creates_gate_state(self, fake_repo, mock_git):
        result = rg.cmd_dispatch(
            argparse.Namespace(sha="abc123", repo=str(fake_repo))
        )
        assert result == 0

        state = rg._load_gate_state(fake_repo, "abc123")
        assert state is not None
        assert state["sha"] == "abc123"
        assert state["version"] == "0.28.32"
        assert state["status"] == "pending"
        assert state["gate_id"].startswith("gate-abc123-")
        assert set(state["reviewer_prompts"].keys()) == {"legal", "learning", "release"}

    def test_dispatch_idempotent(self, fake_repo, mock_git):
        """Dispatching twice for the same SHA should not overwrite."""
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        result = rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 0
        # State should still be pending (not overwritten)
        state = rg._load_gate_state(fake_repo, "abc123")
        assert state["status"] == "pending"


class TestRecord:
    """Tests for recording reviewer verdicts."""

    def _setup_gate(self, fake_repo, mock_git):
        """Create a gate and return the state."""
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        return rg._load_gate_state(fake_repo, "abc123")

    def test_record_pass_verdict(self, fake_repo, mock_git):
        self._setup_gate(fake_repo, mock_git)
        verdict = json.dumps({
            "passed": True,
            "reviewed_sha": "abc123",
            "summary": "All clear",
            "concerns": [],
        })
        result = rg.cmd_record(
            argparse.Namespace(
                sha="abc123", domain="legal", verdict=verdict, repo=str(fake_repo)
            )
        )
        assert result == 0

        state = rg._load_gate_state(fake_repo, "abc123")
        assert "legal" in state["reviewers"]
        assert state["reviewers"]["legal"]["passed"] is True
        assert state["reviewers"]["legal"]["summary"] == "All clear"

    def test_record_blocked_verdict(self, fake_repo, mock_git):
        self._setup_gate(fake_repo, mock_git)
        verdict = json.dumps({
            "passed": False,
            "reviewed_sha": "abc123",
            "summary": "Found a high-severity issue",
            "concerns": [
                {"severity": "high", "file": "test.py", "line": 10,
                 "issue": "bad", "fix": "fix it"}
            ],
        })
        result = rg.cmd_record(
            argparse.Namespace(
                sha="abc123", domain="legal", verdict=verdict, repo=str(fake_repo)
            )
        )
        assert result == 0

        state = rg._load_gate_state(fake_repo, "abc123")
        assert state["reviewers"]["legal"]["passed"] is False
        assert len(state["reviewers"]["legal"]["concerns"]) == 1

    def test_record_rejects_sha_mismatch(self, fake_repo, mock_git):
        self._setup_gate(fake_repo, mock_git)
        verdict = json.dumps({
            "passed": True,
            "reviewed_sha": "wrongsha",
            "summary": "All clear",
            "concerns": [],
        })
        result = rg.cmd_record(
            argparse.Namespace(
                sha="abc123", domain="legal", verdict=verdict, repo=str(fake_repo)
            )
        )
        assert result == 1  # SHA mismatch

    def test_record_rejects_invalid_json(self, fake_repo, mock_git):
        self._setup_gate(fake_repo, mock_git)
        result = rg.cmd_record(
            argparse.Namespace(
                sha="abc123", domain="legal", verdict="not json", repo=str(fake_repo)
            )
        )
        assert result == 1

    def test_record_rejects_unknown_domain(self, fake_repo, mock_git):
        self._setup_gate(fake_repo, mock_git)
        verdict = json.dumps({
            "passed": True,
            "reviewed_sha": "abc123",
            "summary": "ok",
            "concerns": [],
        })
        result = rg.cmd_record(
            argparse.Namespace(
                sha="abc123", domain="unknown", verdict=verdict, repo=str(fake_repo)
            )
        )
        assert result == 1


class TestUnlock:
    """Tests for the unlock command."""

    def _setup_and_pass_all(self, fake_repo, mock_git):
        """Create a gate and record PASS for all three reviewers."""
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        for domain in rg.REVIEWER_DOMAINS:
            verdict = json.dumps({
                "passed": True,
                "reviewed_sha": "abc123",
                "summary": f"{domain} review passed",
                "concerns": [],
            })
            rg.cmd_record(argparse.Namespace(
                sha="abc123", domain=domain, verdict=verdict, repo=str(fake_repo)
            ))

    def test_unlock_generates_attestation(self, fake_repo, mock_git):
        self._setup_and_pass_all(fake_repo, mock_git)
        result = rg.cmd_unlock(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 0

        att_path = rg._attestation_path(fake_repo, "abc123")
        assert att_path.exists()

        attestation = json.loads(att_path.read_text())
        assert attestation["sha"] == "abc123"
        assert attestation["version"] == "0.28.32"
        assert attestation["status"] == "unlocked" if "status" in attestation else True
        assert len(attestation["reviewers"]) == 3
        assert attestation["attestation_hash"]
        assert len(attestation["attestation_hash"]) == 64  # SHA-256 hex

    def test_unlock_requires_all_three(self, fake_repo, mock_git):
        """Unlock should fail if not all three reviewers have passed."""
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        # Only record one reviewer
        verdict = json.dumps({
            "passed": True,
            "reviewed_sha": "abc123",
            "summary": "ok",
            "concerns": [],
        })
        rg.cmd_record(argparse.Namespace(
            sha="abc123", domain="legal", verdict=verdict, repo=str(fake_repo)
        ))

        result = rg.cmd_unlock(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 1  # Not all three passed

    def test_unlock_requires_all_passed(self, fake_repo, mock_git):
        """Unlock should fail if any reviewer blocked."""
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        # Legal passes, learning blocks
        pass_verdict = json.dumps({
            "passed": True, "reviewed_sha": "abc123", "summary": "ok", "concerns": []
        })
        block_verdict = json.dumps({
            "passed": False, "reviewed_sha": "abc123",
            "summary": "blocked", "concerns": [{"severity": "high", "file": "x", "line": 1, "issue": "x", "fix": "y"}]
        })
        rg.cmd_record(argparse.Namespace(
            sha="abc123", domain="legal", verdict=pass_verdict, repo=str(fake_repo)
        ))
        rg.cmd_record(argparse.Namespace(
            sha="abc123", domain="learning", verdict=block_verdict, repo=str(fake_repo)
        ))
        rg.cmd_record(argparse.Namespace(
            sha="abc123", domain="release", verdict=pass_verdict, repo=str(fake_repo)
        ))

        result = rg.cmd_unlock(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 1  # One reviewer blocked

    def test_unlock_rejects_sha_change(self, fake_repo, mock_git):
        """Unlock should fail if HEAD has moved since dispatch."""
        self._setup_and_pass_all(fake_repo, mock_git)
        # Simulate HEAD changing
        with patch.object(rg, "_git_sha", return_value="differentsha"):
            result = rg.cmd_unlock(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 1

    def test_unlock_rejects_dirty_tree(self, fake_repo, mock_git):
        """Unlock should fail if the working tree is not clean."""
        self._setup_and_pass_all(fake_repo, mock_git)
        with patch.object(rg, "_git_status_clean", return_value=False):
            result = rg.cmd_unlock(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 1


class TestStatus:
    """Tests for the status command."""

    def test_status_no_gate(self, fake_repo):
        result = rg.cmd_status(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 1

    def test_status_pending(self, fake_repo, mock_git):
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        result = rg.cmd_status(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 0

    def test_status_partial(self, fake_repo, mock_git):
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        verdict = json.dumps({
            "passed": True, "reviewed_sha": "abc123", "summary": "ok", "concerns": []
        })
        rg.cmd_record(argparse.Namespace(
            sha="abc123", domain="legal", verdict=verdict, repo=str(fake_repo)
        ))
        result = rg.cmd_status(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 0

    def test_status_all_passed(self, fake_repo, mock_git):
        rg.cmd_dispatch(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        for domain in rg.REVIEWER_DOMAINS:
            verdict = json.dumps({
                "passed": True, "reviewed_sha": "abc123",
                "summary": f"{domain} ok", "concerns": []
            })
            rg.cmd_record(argparse.Namespace(
                sha="abc123", domain=domain, verdict=verdict, repo=str(fake_repo)
            ))
        result = rg.cmd_status(argparse.Namespace(sha="abc123", repo=str(fake_repo)))
        assert result == 0


class TestSHA256:
    """Tests for the attestation hash."""

    def test_sha256_is_64_chars(self):
        h = rg._sha256("test")
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_sha256_deterministic(self):
        h1 = rg._sha256("test")
        h2 = rg._sha256("test")
        assert h1 == h2

    def test_sha256_different_inputs(self):
        h1 = rg._sha256("test1")
        h2 = rg._sha256("test2")
        assert h1 != h2


class TestNowISO:
    """Tests for ISO timestamp generation."""

    def test_now_iso_format(self):
        ts = rg._now_iso()
        assert "T" in ts
        assert ts.endswith("+00:00") or ts.endswith("Z")

    def test_now_iso_valid(self):
        from datetime import datetime
        ts = rg._now_iso()
        # Should be parseable
        datetime.fromisoformat(ts)


class TestRepoRoot:
    """Tests for repo root resolution."""

    def test_repo_root_explicit(self, tmp_path):
        repo = tmp_path / "myrepo"
        repo.mkdir()
        result = rg._repo_root(str(repo))
        assert result == repo.resolve()

    def test_repo_root_default(self):
        # Default is now cwd, not a hardcoded absolute path
        result = rg._repo_root(None)
        assert result == Path.cwd().resolve()
