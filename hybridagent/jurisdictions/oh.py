"""Ohio (OH) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 2 (primary_source).
Confidence: primary_source.
Notable: OH follows Daubert; firm registration required (ORC §4733.16);
OH explicitly authorizes digital seals with specific criteria (§4733.14).
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="OH",
    state_name="Ohio",
    board_name="OH State Board of Registration for Professional Engineers and Land Surveyors",
    board_url="https://peps.ohio.gov",
    governing_statute="OH Revised Code Chapter 4733 (Professional Engineers and Land Surveyors)",
    statute_url="https://codes.ohio.gov/orc/4733",
    firm_coa_required=True,
    firm_coa_citation="ORC §4733.16 (firm registration required)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="ORC §4733.14 (digital seals: unique, verifiable, sole control, document-linked)",
    admissibility_standard="daubert",
    admissibility_citation="State v. Steadman, 2015-Ohio-4919 (OH applies Daubert)",
    pdh_hours=30,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="OAC 4733-13 (continuing education; 30 PDH/biennium incl. 2 ethics)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="OH",
    state_name="Ohio",
    bar_name="OH State Bar Association (voluntary); Office of Disciplinary Counsel",
    bar_url="https://www.ohiobar.org",
    governing_rules="Ohio Rules of Professional Conduct",
    rules_url="https://www.supremecourt.ohio.gov/Boards/OPC/rules/",
    cle_required=True,
    cle_hours=24,
    cle_cycle_years=2,
    cle_ethics_hours=2,
    cle_citation="OH Gov. Bar R. X (CLE; 24 credits/2yr incl. 2.5 ethics/yr)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="ORC §1347.12 (breach notification; OH Personal Information Systems)",
    iolta_required=True,
    iolta_authority="OH IOLTA / Ohio State Bar Foundation",
    upl_statute="ORC §4705 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="primary_source",
)
