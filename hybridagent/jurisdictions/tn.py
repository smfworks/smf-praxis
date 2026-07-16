"""Tennessee (TN) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 1 (primary_source).
Confidence: primary_source.
Notable: TN follows Daubert; firm registration required.
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="TN",
    state_name="Tennessee",
    board_name="TN State Board of Architectural and Engineering Examiners",
    board_url="https://www.tn.gov/commerce/boards/architectural-engineering",
    governing_statute="TN Code Annotated, Title 62, Chapter 2 (Professional Engineers)",
    statute_url="https://law.justia.com/codes/tennessee/title-62/chapter-2/",
    firm_coa_required=True,
    firm_coa_citation="T.C.A. §62-2-104 (firm registration required)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="TN Uniform Electronic Transactions Act (T.C.A. §47-10-101)",
    admissibility_standard="daubert",
    admissibility_citation="McDaniel v. CSX Transp., 955 S.W.2d 257 (Tenn. 1997) (Daubert adopted)",
    pdh_hours=24,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="TN Board Rule 0120-02 (continuing education; 24 PDH/biennium incl. 2 ethics)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="TN",
    state_name="Tennessee",
    bar_name="Tennessee Bar Association (voluntary); Board of Professional Responsibility",
    bar_url="https://www.tba.org",
    governing_rules="TN Rules of Professional Conduct",
    rules_url="https://www.tbpr.org/rules-of-professional-conduct",
    cle_required=True,
    cle_hours=15,
    cle_cycle_years=1,
    cle_ethics_hours=1,
    cle_citation="TN CLE Commission rules (15 credits/yr incl. 3 ethics/2yr)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="T.C.A. §47-18-2107 (TN Identity Theft Deterrence Act; breach notification)",
    iolta_required=True,
    iolta_authority="TN IOLTA program / TN Bar Foundation",
    upl_statute="T.C.A. §23-3-603 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="primary_source",
)
