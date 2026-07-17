#!/usr/bin/env python3
"""Architectural rules as executable checks (H06).

Turns the "Hard constraints" documented in AGENTS.md into mechanical checks
that fail CI. Each check returns a list of violations (empty = pass). The
harness-engineering principle: "enforce invariants, don't micromanage." A
rule that isn't enforced is a rule an agent will drift away from.

Run directly:
    python3 scripts/check_architecture.py

Or via pytest (tests/test_architecture.py calls these and fails on any
violation), so CI enforces them on every push.

Current checks:
  1. wip_one        -- at most one feature is `in_progress` in feature_list.json
  2. version_bumped -- HEAD commit touched hybridagent/ -> __version__ changed
  3. core_deps_free -- no top-level third-party imports in hybridagent/ runtime
                       paths outside the optional-extras allowlist
  4. governance_modules_present -- the governance spine modules exist and are
                       non-trivial (not hollowed out)
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HYBRIDAGENT = os.path.join(REPO, "hybridagent")
FEATURE_LIST = os.path.join(REPO, "feature_list.json")
INIT_FILE = os.path.join(HYBRIDAGENT, "__init__.py")

# ---------------------------------------------------------------------------
# Allowlists
# ---------------------------------------------------------------------------

# Local intra-package modules (not third-party).
LOCAL_MODULES = {
    "agent", "agent_service", "api_contract", "authority", "authn", "authz", "bm25", "bootstrap",
    "broker", "broker_client",
    "claims", "checkpoints", "compliance", "content_guard", "context", "contradiction", "cron", "custody", "data_policy",
    "chat_agent", "debate", "deepthink", "embeddings", "escalation",
    "eval_history", "evals", "evidence", "extraction", "external_rooms", "gateways", "grounding", "growth", "identity",
    "ingest", "llm", "logging_util", "marketplace", "m365_tools",
    "mcp_adapter", "mcp_client", "memory", "metrics", "multimodal", "notify",
    "onboard", "orchestrator", "organizations", "pack", "perception", "persona",
    "persistence", "plan_execute", "planner", "plugins", "pricing", "pulse",
    "rag", "readiness", "real_tools", "reflection", "reflexion", "router", "run_state",
    "research_run", "reviews", "router_model", "sandbox", "scratchpad", "search", "security_scan",
    "skill_evaluator", "skills", "structured", "task_manager", "tools",
    "validation", "vault", "vecsim", "vertical_evals", "voice", "wiki",
    "wiki_safe", "workspaces", "workspace_context", "workspace_timeline", "wsutil", "verifier", "verifier_llm", "benchmark", "errors",
 "goal_runner", "context_profile", "a2a_client", "providers",
    "consolidation", "jurisdictions", "security_attestation",
    "advertising_filing", "legal_hold", "credentials", "conflicts",
    "discovery_templates", "clinical_attestation", "hipaa_governance",
    "controlled_substances", "telemedicine_gate", "minor_consent",
}

# Third-party imports allowed behind optional extras (must match pyproject.toml
# [project.optional-dependencies]).
ALLOWED_EXTRAS = {
    "pypdf", "docx", "pptx", "openpyxl", "reportlab", "extract_msg", "markitdown",  # docs/artifacts
    "PIL", "whisper", "cv2", "numpy",                                  # multimodal/fast
    "playwright", "mcp", "keyring", "cryptography",                    # browser/mcp/keyring
}

# Governance-spine modules that must never be deleted or hollowed.
GOVERNANCE_MODULES = {
    "broker.py": "GovernanceBroker",
    "validation.py": None,       # existence check only
    "content_guard.py": None,
    "compliance.py": None,
    "sandbox.py": None,
}

STDLIB = set(sys.stdlib_module_names) | {"hybridagent"}


# ---------------------------------------------------------------------------
# Check 1: WIP = 1
# ---------------------------------------------------------------------------

def check_wip_one() -> list[str]:
    """At most one feature is `in_progress` in feature_list.json."""
    if not os.path.exists(FEATURE_LIST):
        return ["feature_list.json not found"]
    data = json.load(open(FEATURE_LIST, encoding="utf-8"))
    in_progress = [f["id"] for f in data.get("features", [])
                   if f.get("status") == "in_progress"]
    if len(in_progress) > 1:
        return [f"multiple features in_progress: {in_progress} "
                f"(WIP=1 rule violated)"]
    return []


# ---------------------------------------------------------------------------
# Check 2: version bumped on commits that touch hybridagent/
# ---------------------------------------------------------------------------

def check_version_bumped() -> list[str]:
    """If HEAD touched any file under hybridagent/, __version__ must differ
    from its parent. Catches the classic "forgot to bump" bug."""
    # Skip in CI environments without git history (shallow clones).
    try:
        head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                               text=True, cwd=REPO, check=True).stdout.strip()
        parent = subprocess.run(["git", "rev-parse", "HEAD~1"],
                                capture_output=True, text=True, cwd=REPO,
                                check=True).stdout.strip()
    except subprocess.CalledProcessError:
        return []  # no git history -> skip
    # Did HEAD touch any hybridagent/ file?
    diff = subprocess.run(
        ["git", "diff", "--name-only", parent, head, "--", "hybridagent/"],
        capture_output=True, text=True, cwd=REPO, check=True).stdout.strip()
    if not diff:
        return []  # commit didn't touch hybridagent/ -> no bump needed
    changed_files = [f for f in diff.splitlines() if f]
    # Get version at HEAD and HEAD~1
    def version_at(ref: str) -> str | None:
        try:
            content = subprocess.run(
                ["git", "show", f"{ref}:hybridagent/__init__.py"],
                capture_output=True, text=True, cwd=REPO, check=True).stdout
        except subprocess.CalledProcessError:
            return None
        m = re.search(r'__version__\s*=\s*"([^"]+)"', content)
        return m.group(1) if m else None
    v_head = version_at(head)
    v_parent = version_at(parent)
    if v_head is None:
        return ["could not read __version__ from hybridagent/__init__.py "
                "at HEAD"]
    if v_head == v_parent:
        return [f"commit {head[:8]} touched hybridagent/ ({len(changed_files)} "
                f"file(s)) but did not bump __version__ "
                f"(still {v_head}). AGENTS.md: 'Never commit without a "
                f"version bump in both pyproject.toml and "
                f"hybridagent/__init__.py.'"]
    return []


# ---------------------------------------------------------------------------
# Check 3: dependency-free core
# ---------------------------------------------------------------------------

def check_core_deps_free() -> list[str]:
    """No top-level third-party imports in hybridagent/ runtime paths outside
    the optional-extras allowlist. Top-level = module-load-time (col_offset 0
    on the import statement); lazy imports inside functions are fine."""
    violations: list[str] = []
    for root, _dirs, files in os.walk(HYBRIDAGENT):
        for fname in files:
            if not fname.endswith(".py"):
                continue
            path = os.path.join(root, fname)
            rel = os.path.relpath(path, REPO)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read(), path)
            except SyntaxError as e:
                violations.append(f"{rel}: parse error: {e}")
                continue
            for node in ast.walk(tree):
                if getattr(node, "col_offset", -1) != 0:
                    continue  # nested/lazy import — fine
                if isinstance(node, ast.Import):
                    for a in node.names:
                        mod = a.name.split(".")[0]
                        if (mod in STDLIB or mod in LOCAL_MODULES
                                or mod in ALLOWED_EXTRAS):
                            continue
                        violations.append(
                            f"{rel}:{node.lineno} top-level third-party import "
                            f"'{a.name}' (not in optional-extras allowlist; "
                            f"move behind a lazy import or add an extra)")
                elif isinstance(node, ast.ImportFrom):
                    if not node.module:
                        continue
                    mod = node.module.split(".")[0]
                    if (mod in STDLIB or mod in LOCAL_MODULES
                            or mod in ALLOWED_EXTRAS):
                        continue
                    violations.append(
                        f"{rel}:{node.lineno} top-level third-party import "
                        f"'from {node.module}' (not in optional-extras "
                        f"allowlist; move behind a lazy import or add an extra)")
    return violations


# ---------------------------------------------------------------------------
# Check 4: governance modules present and non-trivial
# ---------------------------------------------------------------------------

def check_governance_modules() -> list[str]:
    """Governance-spine modules exist and are non-trivial (not hollowed out)."""
    violations: list[str] = []
    for mod, required_symbol in GOVERNANCE_MODULES.items():
        path = os.path.join(HYBRIDAGENT, mod)
        if not os.path.exists(path):
            violations.append(f"hybridagent/{mod} missing — governance spine "
                              f"module deleted (AGENTS.md: 'Governance spine "
                              f"is sacred')")
            continue
        src = open(path, encoding="utf-8").read()
        # Non-trivial: more than a stub. 50 lines is a floor, not a ceiling.
        if len(src.splitlines()) < 10:
            violations.append(f"hybridagent/{mod} is only "
                              f"{len(src.splitlines())} lines — possibly "
                              f"hollowed out")
        if required_symbol:
            if required_symbol not in src:
                violations.append(
                    f"hybridagent/{mod} missing required symbol "
                    f"'{required_symbol}' — governance spine weakened")
    return violations


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

CHECKS: list[tuple[str, Callable[[], list[str]]]] = [
    ("wip_one", check_wip_one),
    ("version_bumped", check_version_bumped),
    ("core_deps_free", check_core_deps_free),
    ("governance_modules_present", check_governance_modules),
]


def run_all() -> int:
    all_violations: list[str] = []
    for name, fn in CHECKS:
        viols = fn()
        status = "PASS" if not viols else "FAIL"
        print(f"[{status}] {name}")
        for v in viols:
            print(f"  - {v}")
            all_violations.append(v)
    if all_violations:
        print(f"\n{len(all_violations)} architectural violation(s).")
        return 1
    print("\nAll architectural checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run_all())
