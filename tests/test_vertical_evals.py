"""Tests for per-vertical eval packs (p09): registry-driven generation.

Post vertical-extraction cutover: the base ships an empty vertical registry.
These tests verify the base's registry mechanism works correctly with zero
verticals installed. Vertical-specific eval-case tests live in the private
vertical repos (smf-praxis-legal, -medical, -education, -homeschool, -forensic).
"""

from hybridagent import vertical_evals as ve
from hybridagent.broker import RiskClass
from hybridagent.evals import run_evals
from hybridagent.verticals import registry as vertical_registry
from hybridagent.verticals.registry import (
    VerticalSpec,
    clear_registry,
    register_vertical_spec,
)


def test_base_has_zero_vertical_specs(monkeypatch):
    """Base ships with an empty vertical registry (no verticals installed)."""
    clear_registry()
    monkeypatch.setattr(vertical_registry, "_installed_entry_points", lambda: [])
    # Re-trigger the bridge import attempt (will fail in trimmed base)
    import importlib
    importlib.reload(ve)
    cases = ve.vertical_eval_cases()
    assert len(cases) == 0, f"base alone should produce 0 vertical cases, got {len(cases)}"
    assert len(ve.VERTICAL_SPECS) == 0, f"base alone should have 0 specs, got {len(ve.VERTICAL_SPECS)}"


def test_registry_mechanism_works(monkeypatch):
    """Registering a fake vertical spec produces persona + posture cases."""
    clear_registry()
    monkeypatch.setattr(vertical_registry, "_installed_entry_points", lambda: [])
    spec = VerticalSpec(
        name="test_vert",
        persona_keyword="test",
        compliance_mode="enforced",
        autonomous={RiskClass.READ, RiskClass.DRAFT},
        held={RiskClass.SEND, RiskClass.DESTRUCTIVE},
    )
    register_vertical_spec(spec)
    import importlib
    importlib.reload(ve)
    cases = ve.vertical_eval_cases()
    assert len(cases) == 2, f"expected 2 generic cases for 1 spec, got {len(cases)}"
    ids = {c.id for c in cases}
    assert {"vertical.test_vert.persona", "vertical.test_vert.posture"} == ids
    clear_registry()
    importlib.reload(ve)


def test_vertical_category_empty_in_base(monkeypatch):
    """Full eval suite in base has no vertical category (0 vertical cases)."""
    clear_registry()
    monkeypatch.setattr(vertical_registry, "_installed_entry_points", lambda: [])
    import importlib
    importlib.reload(ve)
    report = run_evals()
    cats = report.by_category()
    # vertical category should be absent or 0
    vert = cats.get("vertical", {})
    assert vert.get("total", 0) == 0, f"base should have 0 vertical evals, got {vert}"
    assert report.passed, "base eval suite should pass with no verticals"