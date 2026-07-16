"""Pennsylvania (PA) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 2 (WV/MD/PA/OH/NJ).
Confidence: forensic = primary_source; legal = established_knowledge (PA bar
site retrieval was partial). PDH count and admissibility are well-established.

Notable: PA uses the **Frye** standard for expert testimony (not Daubert) —
one of only two Frye jurisdictions in the 13 (NY is the other). This is the
most significant forensic divergence for a firm using Praxis in PA courts.
"""
from __future__ import annotations

from . import (
    ForensicProfile,
    LegalProfile,
)

FORENSIC = ForensicProfile(
    state="PA",
    state_name="Pennsylvania",
    board_name="Pennsylvania State Registration Board for Professional Engineers, Land Surveyors and Geologists",
    board_url="https://www.dos.pa.gov/ProfessionalLicensing/BoardsCommissions/EngineersLandSurveyorsandGeologists",
    governing_statute="PE Act, 63 P.S. §§ 130-312-318 (Pennsylvania Engineer, Land Surveyor and Geologist Registration Law)",
    statute_url="https://www.legis.state.pa.us/WU01/LI/LI/US/PDF/1992/0/0091..PDF",
    firm_coa_required=True,
    firm_coa_citation="63 P.S. § 130.71 (Certificate of Approval of Name)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="37 PS § 37.60 (digital seals: unique, verifiable, sole control, document-linked)",
    admissibility_standard="frye",
    admissibility_citation="Commonwealth v. Minerd, 819 A.2d 581 (Pa. Super. 2003); PA applies Frye (general acceptance)",
    pdh_hours=24,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="49 PA Code § 47.31 (continuing education)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="PA",
    state_name="Pennsylvania",
    bar_name="Pennsylvania Bar Association (voluntary); Disciplinary Board of PA",
    bar_url="https://www.pabar.org",
    governing_rules="PA Rules of Professional Conduct",
    rules_url="https://www.padisciplinaryboard.org/for-lawyers/rules-of-professional-conduct",
    cle_required=True,
    cle_hours=12,
    cle_cycle_years=1,
    cle_ethics_hours=1,
    cle_citation="PA CLE Board rules (continuing legal education)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="73 P.S. § 2301 (PA Breach of Personal Information Notification Act)",
    iolta_required=True,
    iolta_authority="PA IOLTA Board / PA Lawyers Fund for Client Security",
    upl_statute="42 Pa.C.S. § 2521 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="established_knowledge",
)