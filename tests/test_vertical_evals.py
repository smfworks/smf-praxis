"""Tests for per-vertical eval packs (p09): generation, homeschool coverage, posture."""

from hybridagent import vertical_evals as ve
from hybridagent.evals import run_evals


def test_vertical_cases_generated_for_each_spec():
    cases = ve.vertical_eval_cases()
    ids = {c.id for c in cases}
    assert all(c.category == "vertical" for c in cases)
    # 2 auto-generated (persona + posture) per vertical + manual law-firm + medical-office + school_system cases
    manual_law_firm = 5
    manual_medical_office = 5
    manual_school_system = 5
    assert len(cases) == (
        2 * len(ve.VERTICAL_SPECS)
        + manual_law_firm + manual_medical_office + manual_school_system
    )
    assert {"vertical.homeschool.persona", "vertical.homeschool.posture"} <= ids
    # the law_firm auto cases + manual cases are present
    assert {"vertical.law_firm.persona", "vertical.law_firm.posture"} <= ids
    assert {"vertical.law_firm.upl_guardrail",
            "vertical.law_firm.ny_ad_filing_gate",
            "vertical.law_firm.ma_wisp_attestation",
            "vertical.law_firm.conflict_check",
            "vertical.law_firm.cle_status"} <= ids
    # medical_office auto + manual cases
    assert {"vertical.medical_office.persona", "vertical.medical_office.posture"} <= ids
    assert {"vertical.medical_office.never_write_chart",
            "vertical.medical_office.telemedicine_gate",
            "vertical.medical_office.controlled_substance",
            "vertical.medical_office.minor_consent",
            "vertical.medical_office.portal_triage"} <= ids
    # school_system auto + manual cases
    assert {"vertical.school_system.persona", "vertical.school_system.posture"} <= ids
    assert {"vertical.school_system.draft_not_decide",
            "vertical.school_system.ny_2d_privacy",
            "vertical.school_system.educator_attestation",
            "vertical.school_system.parent_triage",
            "vertical.school_system.vendor_hygiene"} <= ids


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
