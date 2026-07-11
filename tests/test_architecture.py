"""Architectural-rules checks (H06) — pytest wrapper.

Fails on any violation of the executable architectural invariants defined
in scripts/check_architecture.py. CI runs this on every push so the rules
in AGENTS.md are enforced, not just documented.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "check_architecture.py"


def _load_checker():
    """Load scripts/check_architecture.py as a module (it's not in the
    package, so import it by path)."""
    spec = importlib.util.spec_from_file_location("check_architecture", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_architecture"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_architectural_invariants():
    """All architectural checks in scripts/check_architecture.py must pass."""
    checker = _load_checker()
    violations = []
    for _name, fn in checker.CHECKS:
        violations.extend(fn())
    if violations:
        raise AssertionError(
            f"{len(violations)} architectural violation(s):\n  - "
            + "\n  - ".join(violations))


def test_wip_at_most_one_in_progress():
    """WIP=1: at most one feature is in_progress in feature_list.json."""
    checker = _load_checker()
    assert checker.check_wip_one() == [], (
        "WIP=1 violated: " + ", ".join(checker.check_wip_one()))


def test_core_deps_free():
    """No top-level third-party imports in hybridagent/ runtime paths."""
    checker = _load_checker()
    viols = checker.check_core_deps_free()
    assert viols == [], "Core dependency boundary violated:\n  - " + "\n  - ".join(viols)


def test_governance_modules_present():
    """Governance-spine modules exist and are non-trivial."""
    checker = _load_checker()
    viols = checker.check_governance_modules()
    assert viols == [], "Governance spine weakened:\n  - " + "\n  - ".join(viols)