"""Per-jurisdiction regulatory registry tests (Gap 1 — 13-state vertical build).

The registry is the foundation for the Forensic Engineering / Law Firm
vertical build-out: every downstream feature (NY ad-filing, MA WISP
attestation, CE tracking, etc.) loads state-specific rules from here. These
tests pin the key divergences the gap analysis identified so a bad edit to a
state file fails loudly instead of silently shipping wrong compliance facts.
"""
from __future__ import annotations

import pytest

from hybridagent.jurisdictions import (
    get_forensic_profile, get_legal_profile,
    registered_states, forensic_summary, legal_summary,
    ForensicProfile, LegalProfile,
)


# ---------------------------------------------------------------------------
# Registry completeness — all 13 states present with both profiles

def test_registry_covers_all_13_states():
    states = registered_states()
    assert set(states) == {
        "fl", "ga", "sc", "tn", "va", "wv", "md",
        "pa", "oh", "nj", "ny", "ct", "ma",
    }
    assert len(states) == 13


@pytest.mark.parametrize("state", registered_states())
def test_every_state_has_forensic_profile(state):
    p = get_forensic_profile(state)
    assert p is not None, f"{state} missing FORENSIC profile"
    assert isinstance(p, ForensicProfile)
    assert p.state == state.upper()
    assert p.state_name
    assert p.board_name
    assert p.board_url
    assert p.governing_statute
    assert p.confidence in ("primary_source", "established_knowledge", "mixed")


@pytest.mark.parametrize("state", registered_states())
def test_every_state_has_legal_profile(state):
    p = get_legal_profile(state)
    assert p is not None, f"{state} missing LEGAL profile"
    assert isinstance(p, LegalProfile)
    assert p.state == state.upper()
    assert p.bar_name
    assert p.governing_rules
    assert p.confidence in ("primary_source", "established_knowledge", "mixed")


# ---------------------------------------------------------------------------
# Key forensic divergences (the things a bad edit would break silently)

def test_pa_and_ny_use_frye_not_daubert():
    """PA and NY are the only two Frye jurisdictions in the 13. Expert
    testimony in those courts must be defensible under general acceptance,
    not reliability. A regression here would ship wrong admissibility law."""
    assert get_forensic_profile("PA").admissibility_standard == "frye"
    assert get_forensic_profile("NY").admissibility_standard == "frye"


def test_sc_uses_painter_standard():
    """SC is the outlier — its own Painter/Council standard, not Daubert
    or Frye. The only non-Daubert/non-Frye state in the 13."""
    assert get_forensic_profile("SC").admissibility_standard == "painter"


def test_ma_is_the_only_state_without_firm_coa():
    """MA is the only state in the 13 that does not require a firm-level
    Certificate of Authorization. The other 12 require it."""
    ma = get_forensic_profile("MA")
    assert ma.firm_coa_required is False
    for st in registered_states():
        if st == "ma":
            continue
        assert get_forensic_profile(st).firm_coa_required is True, (
            f"{st} should require firm COA")


def test_sc_statute_mentions_expert_testimony():
    """SC is the only state that explicitly includes expert technical
    testimony in the engineering-practice definition."""
    sc = get_forensic_profile("SC")
    assert "expert technical testimony" in sc.forensic_specific_rules.lower()


def test_all_electronic_seals_authorized():
    """All 13 states authorize electronic seals (UETA or state-specific).
    A regression here would break the seal/stamp workflow."""
    for st in registered_states():
        assert get_forensic_profile(st).electronic_seal_authorized is True, (
            f"{st} should authorize electronic seals")


# ---------------------------------------------------------------------------
# Key legal divergences

def test_ma_is_the_only_state_without_mandatory_cle():
    """MA is the only state in the 13 without mandatory CLE. All other 12
    require it (MD requires it too, per batch 2 — just couldn't retrieve hours)."""
    ma = get_legal_profile("MA")
    assert ma.cle_required is False
    assert ma.cle_hours == 0
    for st in registered_states():
        if st == "ma":
            continue
        assert get_legal_profile(st).cle_required is True, (
            f"{st} should require CLE")


def test_only_ny_and_fl_require_advertising_filing():
    """NY and FL are the only two states requiring attorney-advertising filing.
    The other 11 don't."""
    filing_states = [st for st in registered_states()
                     if get_legal_profile(st).advertising_filing_required]
    assert set(filing_states) == {"ny", "fl"}, (
        f"only NY and FL should require filing, got {filing_states}")


def test_ma_is_wisp_mandate_ny_is_shield_rest_are_breach_only():
    """MA 201 CMR 17.00 is the strictest (WISP mandate). NY SHIELD Act adds
    affirmative security duty. The rest are breach-notification-only."""
    assert get_legal_profile("MA").data_security_tier == "wisp_mandate"
    assert get_legal_profile("NY").data_security_tier == "shield_obligation"
    for st in registered_states():
        if st in ("ma", "ny"):
            continue
        assert get_legal_profile(st).data_security_tier == "breach_notification_only", (
            f"{st} should be breach-notification-only")


def test_all_states_prohibit_mdp_and_require_iolta():
    """All 13 prohibit non-lawyer ownership of law firms and require IOLTA.
    These are uniform across the 13 — a regression means a state file lost
    a non-negotiable bar rule."""
    for st in registered_states():
        p = get_legal_profile(st)
        assert p.mdp_prohibited is True, f"{st} must prohibit MDP"
        assert p.iolta_required is True, f"{st} must require IOLTA"
        assert p.upl_statute, f"{st} must have a UPL citation"


# ---------------------------------------------------------------------------
# Confidence tracking — unverified data must be flagged

def test_established_knowledge_states_are_flagged():
    """States whose primary sources couldn't be retrieved (NJ, MD, CT) must
    carry confidence='established_knowledge', not 'primary_source'. Downstream
    features should surface this so a firm doesn't rely on unverified data."""
    nj = get_legal_profile("NJ")
    md = get_legal_profile("MD")
    ct = get_legal_profile("CT")
    for p in (nj, md, ct):
        assert p.confidence == "established_knowledge", (
            f"{p.state} should be established_knowledge (sources were blocked)")


def test_primary_source_states_are_marked():
    """The states with primary-source retrieval (FL, SC, WV, OH, PA, NY, MA)
    should be marked primary_source for the fields that were verified."""
    for st in ("fl", "sc", "wv", "oh", "pa", "ny", "ma"):
        p = get_forensic_profile(st)
        assert p.confidence == "primary_source", (
            f"{st} forensic should be primary_source")


# ---------------------------------------------------------------------------
# Loader edge cases

def test_unknown_state_returns_none():
    assert get_forensic_profile("XX") is None
    assert get_legal_profile("xx") is None
    assert get_forensic_profile("") is None
    assert get_legal_profile("CALIFORNIA") is None  # not in the 13


def test_loader_case_insensitive():
    """Loader accepts both 'PA' and 'pa'."""
    assert get_forensic_profile("PA").state == "PA"
    assert get_forensic_profile("pa").state == "PA"
    assert get_legal_profile("Ny").state == "NY"


def test_summaries_return_all_13_rows():
    fs = forensic_summary()
    ls = legal_summary()
    assert len(fs) == 13
    assert len(ls) == 13
    assert {row["state"] for row in fs} == {s.upper() for s in registered_states()}
    assert all("admissibility" in row for row in fs)
    assert all("data_security" in row for row in ls)