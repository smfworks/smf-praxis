"""Maryland (MD) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 2 (established_knowledge — MD General
Assembly statute API returned errors; primary statute text not retrievable).
Confidence: established_knowledge. Re-verify when web tools are restored.

Notable: MD has NO mandatory CLE (one of very few states without it — MA is the
other in this 13). MD follows Daubert. Firm registration likely required
(unconfirmed — flag for re-verification).
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="MD",
    state_name="Maryland",
    board_name="MD Board for Professional Engineers (DLR)",
    board_url="https://www.dllr.state.md.us/license/pe",
    governing_statute="MD Business Occupations & Professions Code, Title 14 (Professional Engineers)",
    statute_url="https://mgaleg.maryland.gov/Webmga/frmStatutes.aspx?title=14",
    firm_coa_required=True,
    firm_coa_citation="MD firm registration requirements (exact citation to verify)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="MD Uniform Electronic Transactions Act (§9-10A-01)",
    admissibility_standard="daubert",
    admissibility_citation="MD Rule 5-702 (expert testimony; Daubert-influenced reliability standard)",
    pdh_hours=24,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="MD Board continuing education rules (24 PDH/biennium — verify exact hours)",
    forensic_specific_rules="None",
    confidence="established_knowledge",
)


LEGAL = LegalProfile(
    state="MD",
    state_name="Maryland",
    bar_name="MD State Bar Association (voluntary); MD Attorney Grievance Commission",
    bar_url="https://www.msba.org",
    governing_rules="MD Rules of Professional Conduct",
    rules_url="https://www.courts.state.md.us/attorney/rules",
    cle_required=True,
    cle_hours=12,
    cle_cycle_years=1,
    cle_ethics_hours=1,
    cle_citation="MD Supreme Court CLE rules (MD requires CLE; exact hours not retrievable from sources accessed — 12/yr is a placeholder pending verification)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="MD Commercial Law §14-3504 (breach notification; Personal Information Protection Act)",
    iolta_required=True,
    iolta_authority="MD Legal Services Trust Account program / MD Bar Foundation",
    upl_statute="MD Business Occupations Code §10-601 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="established_knowledge",
)
