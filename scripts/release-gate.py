#!/usr/bin/env python3
"""Praxis release gate: automate the three-reviewer exact-SHA review process.

Usage:
    python3 scripts/release-gate.py dispatch <sha> [--repo <path>]
    python3 scripts/release-gate.py status <sha> [--repo <path>]
    python3 scripts/release-gate.py unlock <sha> [--repo <path>]

The gate enforces the Praxis release contract:
  1. Three independent reviewers (legal/privacy, learning/safety, release/integration)
     must each return PASS on the exact immutable SHA.
  2. No push, tag, or publication may occur until all three PASS.
  3. The gate state is persisted to .hermes/release-gates/<sha>.json so it
     survives across sessions.

The tool orchestrates `delegate_task` calls — it does not replace them.
Each reviewer is dispatched as a leaf subagent with a scoped prompt. The
subagent returns JSON with a PASS/BLOCKED verdict. The gate aggregates
verdicts and, when all three PASS, writes a signed attestation file.

The attestation file (.hermes/release-gates/<sha>.attestation.json) contains:
  - sha: the exact commit reviewed
  - version: hybridagent.__version__ at that SHA
  - reviewers: list of {domain, verdict, summary, timestamp}
  - unlocked_at: ISO timestamp when the gate opened
  - gate_id: unique identifier for this gate instance
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Reviewer definitions ---

REVIEWER_DOMAINS = {
    "legal": {
        "title": "Legal / Privacy Reviewer",
        "focus": (
            "Review ONLY for legal, privacy, compliance, and authority-policy "
            "concerns. Check: tenant isolation, data policy, authority "
            "applicability, legal-route binding, household/learner scoping, "
            "PII handling, DNS-rebinding protection, Host header validation, "
            "and compliance with the 13-state matrix. Return JSON only."
        ),
    },
    "learning": {
        "title": "Learning / Safety Reviewer",
        "focus": (
            "Review ONLY for learning-safety, eval correctness, and vertical "
            "eval concerns. Check: child-safe tutoring, responsible-adult "
            "review, no fabrication of attendance/work/assessments, "
            "eval correctness (30/30 open-core base; vertical cases only "
            "when a vertical package is installed), safety guardrails, "
            "authorship binding, and evidence replay. Return JSON only."
        ),
    },
    "release": {
        "title": "Release / Integration Reviewer",
        "focus": (
            "Review ONLY for release-integration concerns. Check: test suite "
            "green (pytest, evals 30/30 on the open-core base, Ruff, mypy), coverage gate >=80%, "
            "architecture 4/4, wheel/sdist + Twine checks, clean-wheel "
            "installation, version bump in __init__.py + pyproject.toml, "
            "no stale artifacts, standard startup path works, and "
            "release-gate attestation is valid. Return JSON only."
        ),
    },
}

# The prompt template for each reviewer subagent.
# This is what gets passed to delegate_task as the `goal`.
REVIEWER_PROMPT_TEMPLATE = """You are an independent reviewer for the Praxis release gate.

**Domain:** {domain_title}
**Focus:** {domain_focus}

**Candidate under review:**
- Repository: {repo}
- Exact SHA: {sha}
- Version: {version}
- Gate ID: {gate_id}

**Your task:**
1. Attest that HEAD equals the full SHA `{sha}`.
2. Attest that `hybridagent.__version__` is `{version}`.
3. Attest that `git status` is clean (no uncommitted changes).
4. Attest that `origin/main` is at the expected baseline.
5. Perform your domain-specific review (see Focus above).
6. Return ONLY JSON with this exact schema:

{{
  "passed": true|false,
  "reviewed_sha": "{sha}",
  "domain": "{domain}",
  "summary": "<one-line summary of your findings>",
  "concerns": [
    {{"severity": "high|medium|low", "file": "<path>", "line": <int>, "issue": "<description>", "fix": "<description>"}}
  ]
}}

Fail closed: if you cannot complete the full review, or if any high/medium
concern remains, set passed=false. Only set passed=true if you have
independently verified the exact SHA and found no high/medium issues in
your domain.
"""


def _repo_root(repo: str | None) -> Path:
    """Resolve the repository root path.

    Defaults to the current working directory (which should be the repo root
    when the script is invoked from inside it). No hardcoded absolute path —
    that would leak infrastructure layout if gate state were committed.
    """
    if repo:
        return Path(repo).resolve()
    return Path.cwd().resolve()


def _gate_dir(repo: Path) -> Path:
    """Directory where gate state files live."""
    return repo / ".hermes" / "release-gates"


def _gate_state_path(repo: Path, sha: str) -> Path:
    """Path to the gate state JSON for a given SHA."""
    return _gate_dir(repo) / f"{sha}.json"


def _attestation_path(repo: Path, sha: str) -> Path:
    """Path to the attestation file for a given SHA."""
    return _gate_dir(repo) / f"{sha}.attestation.json"


def _gate_id(sha: str) -> str:
    """Generate a unique gate ID from the SHA and timestamp."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"gate-{sha[:12]}-{ts}"


def _now_iso() -> str:
    """Current UTC time in ISO 8601."""
    return datetime.now(timezone.utc).isoformat()


def _sha256(text: str) -> str:
    """SHA-256 hex digest of text."""
    return hashlib.sha256(text.encode()).hexdigest()


# --- Git helpers ---

# Timeout for all git subprocess calls. Prevents the release gate from
# hanging indefinitely on a compromised or wedged git process.
_GIT_TIMEOUT = 30


def _git_sha(repo: Path) -> str:
    """Return the full HEAD SHA of the repo."""
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo, capture_output=True, text=True, check=True,
        timeout=_GIT_TIMEOUT,
    )
    return result.stdout.strip()


def _git_status_clean(repo: Path) -> bool:
    """Return True if the working tree is clean."""
    import subprocess
    result = subprocess.run(
        ["git", "status", "--porcelain=v1"],
        cwd=repo, capture_output=True, text=True, check=True,
        timeout=_GIT_TIMEOUT,
    )
    return result.stdout.strip() == ""


def _git_version(repo: Path) -> str:
    """Return hybridagent.__version__ at HEAD."""
    import subprocess
    result = subprocess.run(
        ["git", "show", "HEAD:hybridagent/__init__.py"],
        cwd=repo, capture_output=True, text=True, check=True,
        timeout=_GIT_TIMEOUT,
    )
    # Extract __version__ = "..."
    for line in result.stdout.splitlines():
        if "__version__" in line and "=" in line:
            # e.g. __version__ = "0.28.32"
            return line.split("=")[1].strip().strip('"').strip("'")
    raise RuntimeError("Could not find __version__ in hybridagent/__init__.py")


def _git_origin_main(repo: Path) -> str:
    """Return the SHA of origin/main."""
    import subprocess
    result = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=repo, capture_output=True, text=True, check=True,
        timeout=_GIT_TIMEOUT,
    )
    return result.stdout.strip()


def _git_sha_exists(repo: Path, sha: str) -> bool:
    """Return True if the given SHA is a valid commit in the repo."""
    import subprocess
    try:
        result = subprocess.run(
            ["git", "cat-file", "-t", sha],
            cwd=repo, capture_output=True, text=True, check=True,
            timeout=_GIT_TIMEOUT,
        )
        return result.stdout.strip() == "commit"
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


# --- Gate state management ---

def _load_gate_state(repo: Path, sha: str) -> dict | None:
    """Load gate state from disk, or None if it doesn't exist."""
    path = _gate_state_path(repo, sha)
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _save_gate_state(repo: Path, sha: str, state: dict) -> None:
    """Save gate state to disk."""
    _gate_dir(repo).mkdir(parents=True, exist_ok=True)
    path = _gate_state_path(repo, sha)
    path.write_text(json.dumps(state, indent=2))


def _build_reviewer_prompt(domain: str, repo: Path, sha: str, version: str, gate_id: str) -> str:
    """Build the prompt for a reviewer subagent."""
    info = REVIEWER_DOMAINS[domain]
    return REVIEWER_PROMPT_TEMPLATE.format(
        domain_title=info["title"],
        domain_focus=info["focus"],
        domain=domain,
        repo=str(repo),
        sha=sha,
        version=version,
        gate_id=gate_id,
    )


# --- Commands ---

def cmd_dispatch(args) -> int:
    """Dispatch three independent reviewers for a SHA."""
    repo = _repo_root(args.repo)
    sha = args.sha

    # Verify the SHA exists
    if not _git_sha_exists(repo, sha):
        print(f"ERROR: SHA {sha} not found in {repo}")
        return 1

    # Check if a gate already exists for this SHA
    existing = _load_gate_state(repo, sha)
    if existing:
        print(f"Gate already exists for {sha}:")
        print(f"  Gate ID: {existing['gate_id']}")
        print(f"  Created: {existing['created_at']}")
        print(f"  Status: {existing['status']}")
        print(f"  Reviewers: {', '.join(existing['reviewers'].keys())}")
        print(f"\nUse 'status {sha}' to check progress, or 'unlock {sha}' if all PASS.")
        return 0

    # Gather facts about the candidate
    version = _git_version(repo)
    origin_main = _git_origin_main(repo)

    gate_id = _gate_id(sha)
    state = {
        "gate_id": gate_id,
        "sha": sha,
        "version": version,
        "repo": repo.name,  # store repo dir name, not absolute path (privacy)
        "origin_main": origin_main,
        "created_at": _now_iso(),
        "status": "pending",
        "reviewers": {},
        "reviewer_prompts": {},
    }

    # Build the reviewer prompts (these would be passed to delegate_task)
    for domain in REVIEWER_DOMAINS:
        prompt = _build_reviewer_prompt(domain, repo, sha, version, gate_id)
        state["reviewer_prompts"][domain] = prompt

    _save_gate_state(repo, sha, state)

    print("Release gate created.")
    print(f"  Gate ID: {gate_id}")
    print(f"  SHA: {sha}")
    print(f"  Version: {version}")
    print(f"  Origin/main: {origin_main}")
    print(f"  Created: {state['created_at']}")
    print()
    print("Three reviewers have been queued:")
    for domain, info in REVIEWER_DOMAINS.items():
        print(f"  [{domain}] {info['title']}")
    print()
    print("To dispatch the reviewers, run:")
    print("  python3 -c \"")
    print("from hermes_tools import delegate_task")
    print(f"sha = '{sha}'")
    print(f"gate_id = '{gate_id}'")
    print(f"repo = '{repo}'")
    print(f"version = '{version}'")
    print(f"prompts = json.load(open('{str(_gate_state_path(repo, sha))}'))['reviewer_prompts'])")
    print("tasks = [")
    for domain in REVIEWER_DOMAINS:
        print(f"  {{'goal': prompts['{domain}'], 'context': 'Review domain: {domain}. Gate ID: ' + gate_id}},")
    print("]")
    print("delegate_task(tasks=tasks)")
    print("\"")
    print()
    print("After all three reviewers return, run:")
    print(f"  python3 scripts/release-gate.py unlock {sha} --repo {repo}")

    return 0


def cmd_status(args) -> int:
    """Check the status of a release gate."""
    repo = _repo_root(args.repo)
    sha = args.sha

    state = _load_gate_state(repo, sha)
    if state is None:
        print(f"No gate found for {sha}")
        print(f"Run 'dispatch {sha}' to create one.")
        return 1

    print("Release gate status:")
    print(f"  Gate ID: {state['gate_id']}")
    print(f"  SHA: {state['sha']}")
    print(f"  Version: {state['version']}")
    print(f"  Created: {state['created_at']}")
    print(f"  Status: {state['status']}")
    print()

    reviewers = state.get("reviewers", {})
    if not reviewers:
        print("  No reviewers have returned yet.")
        print("  Dispatch the reviewers using the command from 'dispatch'.")
        return 0

    print("  Reviewers:")
    for domain in REVIEWER_DOMAINS:
        if domain in reviewers:
            r = reviewers[domain]
            verdict = "✓ PASS" if r.get("passed") else "✗ BLOCKED"
            print(f"    [{domain}] {verdict} — {r.get('summary', 'no summary')}")
        else:
            print(f"    [{domain}] pending")

    # Check if all three have returned
    completed = [d for d in REVIEWER_DOMAINS if d in reviewers]
    passed = [d for d in completed if reviewers[d].get("passed")]

    print()
    print(f"  Completed: {len(completed)}/3")
    print(f"  Passed: {len(passed)}/3")

    if len(passed) == 3:
        print()
        print("  All three reviewers PASSED. Run 'unlock' to generate the attestation.")
        return 0
    elif len(completed) == 3:
        print()
        print("  All three reviewers have returned, but not all PASSED.")
        print("  The gate is BLOCKED. Fix the issues and re-dispatch.")
        return 1
    else:
        print()
        print(f"  {3 - len(completed)} reviewer(s) still pending.")
        return 0


def cmd_unlock(args) -> int:
    """Generate the attestation file if all three reviewers PASSED."""
    repo = _repo_root(args.repo)
    sha = args.sha

    state = _load_gate_state(repo, sha)
    if state is None:
        print(f"No gate found for {sha}")
        print(f"Run 'dispatch {sha}' to create one.")
        return 1

    reviewers = state.get("reviewers", {})
    passed = [d for d in REVIEWER_DOMAINS if d in reviewers and reviewers[d].get("passed")]

    if len(passed) < 3:
        print(f"Gate for {sha} is not yet unlocked.")
        print(f"  {len(passed)}/3 reviewers have passed.")
        print(f"  Run 'status {sha}' to check progress.")
        return 1

    # Verify the SHA is still the current HEAD and tree is clean
    current_sha = _git_sha(repo)
    if current_sha != sha:
        print(f"ERROR: Current HEAD ({current_sha}) does not match the gated SHA ({sha}).")
        print("The candidate has changed. Re-dispatch the gate for the new SHA.")
        return 1

    if not _git_status_clean(repo):
        print("ERROR: Working tree is not clean. Commit or stash changes before unlocking.")
        return 1

    # Generate the attestation
    attestation = {
        "gate_id": state["gate_id"],
        "sha": sha,
        "version": state["version"],
        "repo": state["repo"],
        "origin_main": state["origin_main"],
        "created_at": state["created_at"],
        "unlocked_at": _now_iso(),
        "reviewers": [
            {
                "domain": d,
                "title": REVIEWER_DOMAINS[d]["title"],
                "passed": True,
                "summary": reviewers[d].get("summary", ""),
                "concerns": reviewers[d].get("concerns", []),
                "reviewed_sha": reviewers[d].get("reviewed_sha", ""),
            }
            for d in REVIEWER_DOMAINS if d in reviewers and reviewers[d].get("passed")
        ],
        "attestation_hash": "",  # filled below
    }

    # Compute the attestation hash (covers all fields except the hash itself)
    attestation_str = json.dumps(attestation, sort_keys=True, indent=2)
    attestation["attestation_hash"] = _sha256(attestation_str)

    # Write the attestation file
    _gate_dir(repo).mkdir(parents=True, exist_ok=True)
    att_path = _attestation_path(repo, sha)
    att_path.write_text(json.dumps(attestation, indent=2))

    # Update gate state
    state["status"] = "unlocked"
    state["unlocked_at"] = _now_iso()
    state["attestation_path"] = str(att_path)
    _save_gate_state(repo, sha, state)

    print("Release gate UNLOCKED.")
    print(f"  Gate ID: {state['gate_id']}")
    print(f"  SHA: {sha}")
    print(f"  Version: {state['version']}")
    print(f"  Attestation: {att_path}")
    print()
    print("All three reviewers PASSED. You may now push, tag, and publish.")
    print()
    print("Push and tag commands:")
    print("  git checkout main")
    print("  git pull --ff-only origin main")
    print(f"  git push origin {sha}")
    print(f"  git tag v{state['version']}")
    print(f"  git push origin v{state['version']}")
    print()
    print("The GitHub Release workflow will build and attach artifacts.")
    print(f"Verify: curl -sI https://github.com/smfworks/smf-praxis/releases/tag/v{state['version']}")

    return 0


def cmd_record(args) -> int:
    """Record a reviewer's verdict (used by the subagent callback)."""
    repo = _repo_root(args.repo)
    sha = args.sha
    domain = args.domain

    if domain not in REVIEWER_DOMAINS:
        print(f"ERROR: unknown domain '{domain}'. Must be one of: {', '.join(REVIEWER_DOMAINS)}")
        return 1

    state = _load_gate_state(repo, sha)
    if state is None:
        print(f"No gate found for {sha}")
        return 1

    # Read the verdict from stdin or --verdict
    if args.verdict:
        verdict_text = args.verdict
    else:
        verdict_text = sys.stdin.read()

    # Parse the JSON verdict
    try:
        verdict = json.loads(verdict_text)
    except json.JSONDecodeError:
        print("ERROR: could not parse JSON verdict")
        return 1

    # Validate the verdict
    if "passed" not in verdict:
        print("ERROR: verdict must contain 'passed' field")
        return 1

    if verdict.get("reviewed_sha") != sha:
        print(f"ERROR: verdict SHA ({verdict.get('reviewed_sha')}) does not match gated SHA ({sha})")
        return 1

    # Record the verdict
    state["reviewers"][domain] = {
        "passed": verdict["passed"],
        "summary": verdict.get("summary", ""),
        "concerns": verdict.get("concerns", []),
        "reviewed_sha": verdict.get("reviewed_sha", ""),
        "recorded_at": _now_iso(),
    }

    _save_gate_state(repo, sha, state)

    status = "PASS" if verdict["passed"] else "BLOCKED"
    print(f"Recorded {domain} reviewer verdict: {status}")
    print(f"  Summary: {verdict.get('summary', '')}")

    # Check if all three are done
    passed = [d for d in REVIEWER_DOMAINS if d in state["reviewers"] and state["reviewers"][d].get("passed")]
    completed = [d for d in REVIEWER_DOMAINS if d in state["reviewers"]]

    if len(completed) == 3:
        if len(passed) == 3:
            print()
            print("All three reviewers PASSED. Run 'unlock' to generate the attestation.")
        else:
            print()
            print("All three reviewers have returned, but not all PASSED.")
            print("The gate is BLOCKED.")

    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="release-gate",
        description="Praxis release gate: automate the three-reviewer exact-SHA review process.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_disp = sub.add_parser("dispatch", help="create a release gate for a SHA")
    p_disp.add_argument("sha", help="exact commit SHA to gate")
    p_disp.add_argument("--repo", default=None, help="repository path (default: current working directory)")
    p_disp.set_defaults(func=cmd_dispatch)

    p_stat = sub.add_parser("status", help="check gate status for a SHA")
    p_stat.add_argument("sha", help="exact commit SHA")
    p_stat.add_argument("--repo", default=None, help="repository path")
    p_stat.set_defaults(func=cmd_status)

    p_unl = sub.add_parser("unlock", help="generate attestation if all reviewers PASSED")
    p_unl.add_argument("sha", help="exact commit SHA")
    p_unl.add_argument("--repo", default=None, help="repository path")
    p_unl.set_defaults(func=cmd_unlock)

    p_rec = sub.add_parser("record", help="record a reviewer's JSON verdict")
    p_rec.add_argument("sha", help="exact commit SHA")
    p_rec.add_argument("domain", choices=list(REVIEWER_DOMAINS.keys()),
                       help="reviewer domain")
    p_rec.add_argument("--verdict", default=None,
                       help="JSON verdict (or read from stdin)")
    p_rec.add_argument("--repo", default=None, help="repository path")
    p_rec.set_defaults(func=cmd_record)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
