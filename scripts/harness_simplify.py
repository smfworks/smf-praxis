#!/usr/bin/env python3
"""Harness simplification cadence (H09) — monthly component-removal test.

Course L12: "as models improve, harness assumptions go stale." Every month,
disable one harness component, run the benchmark, and compare against the
baseline. If no degradation, the component is overhead — remove it
permanently. If degradation, restore it (or replace with a lighter
alternative). This keeps the harness from accumulating stale constraints.

This script automates the compare-and-decide half:
  1. Run the eval suite against the current (full) harness -> baseline.
  2. Disable a named component via an env var the component honors.
  3. Re-run the eval suite -> degraded result.
  4. Compare baseline vs. degraded; print a decision (KEEP / REMOVE).

Components that can be toggled (each honors an env var that disables it at
runtime without code changes):
  * verifier          PRAXIS_VERIFY=0      disables the H05 critic gate
  * reflexion         PRAXIS_REFLECT=0     disables the dead-end retry
  * context_compact   PRAXIS_CTX_BUDGET=0  disables context compaction
  * goal_loop         (no env yet)         the H10 loop is not env-toggled

Usage:
    # Set the full-harness baseline first (run once, or after a model swap)
    python3 scripts/harness_simplify.py --baseline

    # Test whether a component is overhead
    python3 scripts/harness_simplify.py --component verifier
    python3 scripts/harness_simplify.py --component reflexion
    python3 scripts/harness_simplify.py --component context_compact

The script prints a decision and appends a row to
docs/harness/quality-document.md (simplification log table) so the cadence
is recorded where the course expects it.

Exit codes:
    0 = no degradation (component is overhead; safe to remove)
    1 = degradation detected (keep the component)
    2 = error (baseline missing, unknown component, eval failure)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ENV_FOR_COMPONENT: dict[str, str] = {
    "verifier": "PRAXIS_VERIFY=0",
    "reflexion": "PRAXIS_REFLECT=0",
    "context_compact": "PRAXIS_CTX_BUDGET=0",
}
QUALITY_DOC = REPO / "docs" / "harness" / "quality-document.md"


def _run_eval(env: dict[str, str] | None = None) -> tuple[int, int]:
    """Run `praxis eval --json` and return (passes, total)."""
    cmd = [sys.executable, "-m", "hybridagent.cli", "eval", "--json"]
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=REPO,
                       env=full_env, timeout=120)
    if r.returncode not in (0, 1):  # eval returns 1 on a fail, that's fine
        print(f"eval failed (exit {r.returncode}):\n{r.stderr}", file=sys.stderr)
        sys.exit(2)
    try:
        data = json.loads(r.stdout)
    except json.JSONDecodeError as e:
        print(f"eval output not JSON: {e}", file=sys.stderr)
        sys.exit(2)
    return data.get("passes", 0), data.get("total", 0)


def _set_baseline() -> int:
    """Run the eval suite with the full harness and print the baseline."""
    passes, total = _run_eval()
    print(f"baseline (full harness): {passes}/{total}")
    if passes != total:
        print("WARNING: baseline is not green; fix before simplifying.",
              file=sys.stderr)
        return 1
    return 0


def _test_component(component: str) -> int:
    """Disable one component, re-run eval, compare, print decision, log it."""
    if component not in ENV_FOR_COMPONENT:
        print(f"unknown component '{component}'. Known: "
              f"{', '.join(sorted(ENV_FOR_COMPONENT))}", file=sys.stderr)
        return 2
    # Baseline first
    base_pass, total = _run_eval()
    # Disable the component
    env_str = ENV_FOR_COMPONENT[component]
    key, _, val = env_str.partition("=")
    deg_pass, deg_total = _run_eval({key: val})
    delta = deg_pass - base_pass
    decision = "REMOVE" if delta == 0 else "KEEP"
    print(f"\n=== H09 simplification test: {component} ===")
    print(f"baseline (full):  {base_pass}/{total}")
    print(f"disabled ({env_str}): {deg_pass}/{deg_total}")
    print(f"delta: {delta:+d}")
    print(f"decision: {decision}")
    if decision == "KEEP":
        print(f"  {component} prevented {abs(delta)} eval regression(s); "
              f"keep it (or replace with a lighter alternative).")
    else:
        print(f"  {component} is overhead at the current model capability; "
              f"safe to remove permanently. File an issue to remove it.")
    _log_to_quality_doc(component, delta, decision)
    return 0 if decision == "REMOVE" else 1


def _log_to_quality_doc(component: str, delta: int, decision: str) -> None:
    """Append a row to the simplification log in quality-document.md."""
    if not QUALITY_DOC.exists():
        return
    row = (f"| {date.today().isoformat()} | {component} | "
           f"{'0' if delta == 0 else f'{delta:+d}'} | {decision} | "
           f"H09 cadence script |")
    src = QUALITY_DOC.read_text(encoding="utf-8")
    # Find the simplification-log table and append after its header row.
    marker = "| Date | Component disabled | Eval delta | Decision | Notes |"
    if marker in src:
        src = src.replace(marker, marker + "\n" + row, 1)
        QUALITY_DOC.write_text(src, encoding="utf-8")
        print(f"\nlogged to {QUALITY_DOC.relative_to(REPO)}")


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if args[0] == "--baseline":
        return _set_baseline()
    if args[0] == "--component" and len(args) > 1:
        return _test_component(args[1])
    print(f"usage: {sys.argv[0]} --baseline | --component <name>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())