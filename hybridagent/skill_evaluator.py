"""Skill evaluation, quality scoring, and quarantine helpers."""
from __future__ import annotations


class SkillEvaluator:
    def __init__(self, library) -> None:
        self.library = library

    def record(self, skill_name: str, goal: str, outcome: str,
               cycle_id: str = "", notes: str = "") -> dict | None:
        self.library.record_outcome(
            skill_name, goal, outcome, cycle_id=cycle_id, notes=notes)
        return self.library.metadata(skill_name)

    def quarantine_low_quality(self, min_uses: int = 3,
                               threshold: float = 0.4) -> list[str]:
        return self.library.quarantine_low_quality(min_uses, threshold)

    def impact_report(self, skill_name: str) -> str:
        meta = self.library.metadata(skill_name)
        if not meta:
            return f"no outcome data for {skill_name}"
        return (
            f"{skill_name}: uses={meta['usage_count']} "
            f"success={meta['success_count']} failure={meta['failure_count']} "
            f"quality={meta['quality_score']:.2f} "
            f"quarantined={bool(meta['quarantined'])}"
        )
