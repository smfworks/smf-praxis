"""Reliability benchmarking on top of the eval suite (Phase D / G13).

The eval suite (`evals.py`) answers "does it pass?"; this answers "how *reliably*
does it pass?" — the dimension OpenClaw's clawbench highlights. It runs the suite
``k`` times and computes reliability statistics that a single pass/fail hides:

* **pass@1 / pass^k** — fraction of cases passing once, vs. passing on *every*
  one of k runs (the honest "can I depend on this" number).
* **per-case flakiness** — cases that don't pass all k runs (the real risk).
* **variance** — spread of per-run pass counts (0 = perfectly stable).

Because Praxis's eval suite is deterministic by design (mock LLM + real
governance), a healthy result is **zero variance / pass^k == pass@1**: any
flakiness this surfaces is a genuine nondeterminism bug worth fixing. With a real
provider (`--real`) it becomes a true reliability probe.

Stdlib-only statistics (no numpy). Output is JSON-friendly for CI gating.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ReliabilityReport:
    k: int
    total_cases: int
    per_run_passes: list[int] = field(default_factory=list)
    flaky_cases: dict[str, int] = field(default_factory=dict)  # case_id -> #passes
    always_pass: int = 0
    always_fail: int = 0

    @property
    def pass_at_1(self) -> float:
        """Mean fraction passing across runs."""
        if not self.per_run_passes or not self.total_cases:
            return 0.0
        return sum(self.per_run_passes) / (len(self.per_run_passes) * self.total_cases)

    @property
    def pass_hat_k(self) -> float:
        """Fraction of cases that pass on EVERY one of the k runs."""
        if not self.total_cases:
            return 0.0
        return self.always_pass / self.total_cases

    @property
    def variance(self) -> float:
        xs = self.per_run_passes
        if len(xs) < 2:
            return 0.0
        mean = sum(xs) / len(xs)
        return sum((x - mean) ** 2 for x in xs) / len(xs)

    @property
    def stable(self) -> bool:
        """Deterministic suite health: no flaky cases across the k runs."""
        return not self.flaky_cases

    def to_dict(self) -> dict:
        return {
            "k": self.k, "total_cases": self.total_cases,
            "pass_at_1": round(self.pass_at_1, 4),
            "pass_hat_k": round(self.pass_hat_k, 4),
            "variance": round(self.variance, 4),
            "per_run_passes": self.per_run_passes,
            "always_pass": self.always_pass, "always_fail": self.always_fail,
            "flaky_cases": self.flaky_cases, "stable": self.stable,
        }

    def summary(self) -> str:
        return (f"k={self.k} cases={self.total_cases} "
                f"pass@1={self.pass_at_1:.3f} pass^{self.k}={self.pass_hat_k:.3f} "
                f"variance={self.variance:.3f} "
                f"{'STABLE' if self.stable else f'FLAKY({len(self.flaky_cases)})'}")


def run_reliability(k: int = 5, category: str | None = None,
                    timeout: float | None = 20.0) -> ReliabilityReport:
    """Run the eval suite ``k`` times and aggregate reliability statistics."""
    from .evals import run_evals
    k = max(1, int(k))
    per_run: list[int] = []
    pass_counts: dict[str, int] = {}
    total = 0
    for _ in range(k):
        report = run_evals(category=category, timeout=timeout)
        total = report.total
        per_run.append(report.passes)
        for r in report.results:
            pass_counts[r.case_id] = pass_counts.get(r.case_id, 0) + (1 if r.passed else 0)
    always_pass = sum(1 for c in pass_counts.values() if c == k)
    always_fail = sum(1 for c in pass_counts.values() if c == 0)
    flaky = {cid: c for cid, c in pass_counts.items() if 0 < c < k}
    return ReliabilityReport(
        k=k, total_cases=total, per_run_passes=per_run,
        flaky_cases=flaky, always_pass=always_pass, always_fail=always_fail)
