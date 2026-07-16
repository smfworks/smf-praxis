"""Virginia (VA) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 1 (primary_source, general provisions retrieved).
Confidence: primary_source (forensic); established_knowledge (legal details).
Notable: VA follows Daubert; firm registration required; VA uses the UBE.
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="VA",
    state_name="Virginia",
    board_name="VA Board for Architects, Professional Engineers, Land Surveyors, Certified Interior Designers and Landscape Architects (APELSCIDLA)",
    board_url="https://www.dpor.virginia.gov/Boards/APELSCIDLA",
    governing_statute="VA Code Title 54.1, Chapter 2 (Professions and Occupations; Architecture, Engineering, etc.)",
    statute_url="https://law.lis.virginia.gov/vacodefull/title54.1/CHAPTER2/",
    firm_coa_required=True,
    firm_coa_citation="54.1-402 (firm registration required)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="VA Uniform Electronic Transactions Act (§59.1-471)",
    admissibility_standard="daubert",
    admissibility_citation="John v. Im, 263 Va. 233 (2002) (VA applies Daubert)",
    pdh_hours=16,
    pdh_cycle_years=1,
    pdh_ethics_hours=2,
    pdh_citation="18VAC10-20 (VA Board regulation; 16 PDH/yr incl. 2 ethics)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="VA",
    state_name="Virginia",
    bar_name="Virginia State Bar (integrated, mandatory)",
    bar_url="https://www.vsb.org",
    governing_rules="Virginia Rules of Professional Conduct",
    rules_url="https://www.vsb.org/site/regulation/rules-of-professional-conduct",
    cle_required=True,
    cle_hours=12,
    cle_cycle_years=1,
    cle_ethics_hours=2,
    cle_citation="VA State Bar CLE rules (12 credits/yr incl. 2 ethics)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="VA Code §18.2-186.6 (breach notification; Identity Theft Protection Act)",
    iolta_required=True,
    iolta_authority="VA IOLTA / VA Law Foundation",
    upl_statute="VA Code §54.1-3904 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="established_knowledge",
)
