"""Eval regression gate — turn the scorecard into a durable, comparable signal.

``praxis eval`` scores capability and safety cases; on its own each run is a
pass/fail snapshot. :func:`compare_reports` diffs a saved baseline against the
current run and flags **regressions** — cases that passed in the baseline but now
fail — so silent capability loss fails CI even when the overall suite still
"passes" (for example because new cases were added that mask the loss). It also
surfaces *fixes* (newly passing) and *added*/*removed* cases.

Both inputs are the plain dicts produced by ``EvalReport.to_dict()``, so this
module has no dependency on the eval harness itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RegressionReport:
    regressions: list[str] = field(default_factory=list)  # passed in base, fail now
    fixes: list[str] = field(default_factory=list)        # failed in base, pass now
    added: list[str] = field(default_factory=list)        # only in current
    removed: list[str] = field(default_factory=list)      # only in baseline

    @property
    def ok(self) -> bool:
        """True when nothing that used to pass now fails."""
        return not self.regressions

    def render(self) -> str:
        head = "OK — no regressions vs baseline." if self.ok else "REGRESSION DETECTED."
        lines: list[str] = []
        if self.regressions:
            lines.append(f"  regressions ({len(self.regressions)}): "
                         + ", ".join(sorted(self.regressions)))
        if self.fixes:
            lines.append(f"  fixed ({len(self.fixes)}): "
                         + ", ".join(sorted(self.fixes)))
        if self.added:
            lines.append(f"  new ({len(self.added)}): " + ", ".join(sorted(self.added)))
        if self.removed:
            lines.append(f"  removed ({len(self.removed)}): "
                         + ", ".join(sorted(self.removed)))
        return head + ("\n" + "\n".join(lines) if lines else "")


def _passed_map(report: dict) -> dict[str, bool]:
    return {c["id"]: bool(c.get("passed"))
            for c in report.get("cases", [])
            if isinstance(c, dict) and "id" in c}


def compare_reports(baseline: dict, current: dict) -> RegressionReport:
    """Diff two ``EvalReport.to_dict()`` snapshots by per-case pass/fail."""
    base = _passed_map(baseline)
    curr = _passed_map(current)
    rr = RegressionReport()
    for cid, was_pass in base.items():
        if cid not in curr:
            rr.removed.append(cid)
        elif was_pass and not curr[cid]:
            rr.regressions.append(cid)
        elif not was_pass and curr[cid]:
            rr.fixes.append(cid)
    rr.added = [cid for cid in curr if cid not in base]
    return rr
