#!/usr/bin/env python
"""Mutation-test the governance core (``hybridagent/broker.py``) with cosmic-ray.

The broker is Praxis's control plane: allowlist, risk routing, dual-approval /
four-eyes, kill-switch, prompt-injection screening, redaction, and the audit
trail. High line coverage there is necessary but not sufficient — we also want
evidence the *tests* would catch a regression. Mutation testing injects small
faults (flip a comparison, swap a risk class, drop an ``append``) and checks
that a test fails ("kills" the mutant). Survivors are gaps in the oracle.

We use **cosmic-ray** (cross-platform, modern-Python clean) instead of mutmut
(needs WSL on Windows) or mutatest (broken on Python 3.11+). The oracle is the
dense :mod:`tests.test_broker_mutation_guard` suite, kept tight so each mutant
runs in ~2s. Config lives in ``mutation.toml``.

**Equivalent mutants.** ``broker.py`` uses ``from __future__ import annotations``,
so every ``X | None`` type annotation is an un-evaluated string. Mutating the
``|`` (cosmic-ray's ``ReplaceBinaryOperator_BitOr_*`` operators) therefore can
never change behaviour — those are *equivalent mutants* that no test can kill.
cosmic-ray's own ``cr-filter-operators`` is broken in the current build (it
crashes reading the session db), so instead of filtering at init time we run the
full session and classify afterwards: BitOr survivors are reported separately and
excluded from the gated "real" survival rate. Any *other* survivor is a genuine
gap to investigate.

Usage::

    python scripts/mutation_test.py                 # full run + classified report
    python scripts/mutation_test.py --fail-over 10  # gate: fail if real rate >10%

Requires the optional extra::  pip install -e ".[mutation]"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = "mutation.toml"
# Keep the cosmic-ray session db OUTSIDE the repo: the repo may live under a
# OneDrive-synced folder, and OneDrive holds SQLite files open, which breaks
# cosmic-ray's delete/reinit between runs. The system temp dir is local + free.
SESSION = os.environ.get(
    "PRAXIS_MUTATION_SESSION",
    os.path.join(tempfile.gettempdir(), "praxis-mutation-session.sqlite"),
)

# cosmic-ray operator families that are provably equivalent here (see module
# docstring): the only `|` BinOps in broker.py are PEP 604 string annotations.
_EQUIVALENT_OPERATORS = ("BitOr",)


def _run(cmd: list[str]) -> int:
    print("[mutation] $", " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=ROOT)


def _classify(session_path: str) -> dict:
    """Parse `cosmic-ray dump` into killed / real-survivor / equivalent counts."""
    out = subprocess.run(["cosmic-ray", "dump", session_path],
                         cwd=ROOT, capture_output=True, text=True).stdout
    killed = incompetent = equivalent = 0
    real_survivors: list[tuple[int, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if not (isinstance(rec, list) and len(rec) == 2):
            continue
        spec, result = rec
        if not result:
            continue
        outcome = result.get("test_outcome")
        if outcome == "killed":
            killed += 1
        elif outcome == "incompetent":
            incompetent += 1
        elif outcome == "survived":
            muts = spec.get("mutations", [{}])
            op = muts[0].get("operator_name", "")
            line_no = (muts[0].get("start_pos") or [None])[0]
            if any(tag in op for tag in _EQUIVALENT_OPERATORS):
                equivalent += 1
            else:
                real_survivors.append((line_no, op.split("/")[-1]))
    real_survivors.sort()
    return {
        "killed": killed,
        "incompetent": incompetent,
        "equivalent": equivalent,
        "real_survivors": real_survivors,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--fail-over", type=float, default=None, metavar="PCT",
                    help="exit non-zero if the REAL survival rate exceeds PCT percent")
    ap.add_argument("--keep-session", action="store_true",
                    help="reuse an existing cosmic-ray session db (resume a run)")
    args = ap.parse_args()

    session_path = SESSION if os.path.isabs(SESSION) else os.path.join(ROOT, SESSION)
    fresh = not (os.path.exists(session_path) and args.keep_session)
    if os.path.exists(session_path) and fresh:
        try:
            os.remove(session_path)
        except OSError as exc:
            print(f"[mutation] could not remove stale session {session_path}: {exc}\n"
                  "           close any cosmic-ray process holding it, or set "
                  "PRAXIS_MUTATION_SESSION to a new path.")
            return 1

    # 1) Baseline: the suite MUST pass against unmutated code, else results lie.
    if _run(["cosmic-ray", "baseline", CONFIG]) != 0:
        print("[mutation] baseline failed — fix the tests before mutating.")
        return 1

    # 2) Enumerate mutants, then 3) execute them.
    if fresh and _run(["cosmic-ray", "init", CONFIG, session_path]) != 0:
        return 1
    if _run(["cosmic-ray", "exec", CONFIG, session_path]) != 0:
        return 1

    # 4) Classify outcomes, separating equivalent annotation mutants.
    stats = _classify(session_path)
    killed = stats["killed"]
    equivalent = stats["equivalent"]
    real = stats["real_survivors"]
    denom = killed + len(real)
    real_rate = (len(real) / denom * 100.0) if denom else 0.0

    print("\n========== mutation summary (hybridagent/broker.py) ==========")
    print(f"  killed:                 {killed}")
    print(f"  equivalent (excluded):  {equivalent}   [PEP 604 annotation BitOr]")
    print(f"  incompetent:            {stats['incompetent']}")
    print(f"  REAL survivors:         {len(real)}")
    for line_no, op in real:
        print(f"      - line {line_no}: {op}")
    print(f"  REAL survival rate:     {real_rate:.1f}%  "
          f"(kill rate {100.0 - real_rate:.1f}%)")
    print("==============================================================")

    if args.fail_over is not None and real_rate > args.fail_over:
        print(f"[mutation] REAL survival rate {real_rate:.1f}% exceeds "
              f"{args.fail_over}% — add tests to kill the survivors above.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
