"""Tests for per-vertical eval packs (p09): generation, homeschool coverage, posture."""

from hybridagent import vertical_evals as ve
from hybridagent.evals import run_evals


def test_vertical_cases_generated_for_each_spec():
    cases = ve.vertical_eval_cases()
    ids = {c.id for c in cases}
    assert all(c.category == "vertical" for c in cases)
    assert len(cases) == 2 * len(ve.VERTICAL_SPECS)  # persona + posture per vertical
    assert {"vertical.homeschool.persona", "vertical.homeschool.posture"} <= ids


def test_homeschool_vertical_pack_passes():
    report = run_evals(category="vertical")
    by_id = {r.case_id: r for r in report.results}
    assert by_id["vertical.homeschool.persona"].passed, by_id["vertical.homeschool.persona"].detail
    assert by_id["vertical.homeschool.posture"].passed, by_id["vertical.homeschool.posture"].detail
    assert report.passed


def test_vertical_category_included_in_full_suite():
    report = run_evals()
    cats = report.by_category()
    assert "vertical" in cats
    assert cats["vertical"]["pass"] == cats["vertical"]["total"]
