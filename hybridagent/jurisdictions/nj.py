"""New Jersey (NJ) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 2 (established_knowledge — NJ
legislative site requires JS; NJ Consumer Affairs/Courts blocked by Incapsula).
Confidence: established_knowledge. Re-verify when web tools are restored.

Notable: NJ follows Daubert; firm registration likely required; NJ has mandatory
CLE (24 credits/2yr incl. ethics). The least-verified of the 13 states.
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="NJ",
    state_name="New Jersey",
    board_name="NJ State Board of Professional Engineers and Land Surveyors (Division of Consumer Affairs)",
    board_url="https://www.njconsumeraffairs.gov/eng/Pages/default.aspx",
    governing_statute="NJ Statutes, Title 45, Chapter 8 (Professional Engineers and Land Surveyors)",
    statute_url="https://www.njleg.state.nj.us/",
    firm_coa_required=True,
    firm_coa_citation="N.J.S.A. 45:8-28 (firm Certificate of Authorization — verify exact section)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="NJ Uniform Electronic Transactions Act (N.J.S.A. 12A-12)",
    admissibility_standard="daubert",
    admissibility_citation="State v. Harvey, 151 N.J. 117 (1997) (NJ applies Daubert)",
    pdh_hours=24,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="N.J.A.C. 13:40 (continuing education; 24 PDH/biennium incl. 2 ethics)",
    forensic_specific_rules="None",
    confidence="established_knowledge",
)


LEGAL = LegalProfile(
    state="NJ",
    state_name="New Jersey",
    bar_name="NJ State Bar Association (voluntary); NJ Office of Attorney Ethics",
    bar_url="https://www.njsba.com",
    governing_rules="NJ Rules of Professional Conduct",
    rules_url="https://www.njcourts.gov/attorneys/ethics-advisory-committee",
    cle_required=True,
    cle_hours=24,
    cle_cycle_years=2,
    cle_ethics_hours=4,
    cle_citation="NJ BCLE rules (24 credits/2yr incl. 4 ethics)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="N.J.S.A. 56:8-163 (breach notification; NJ Consumer Fraud Act)",
    iolta_required=True,
    iolta_authority="NJ IOLTA Fund / NJ State Bar Foundation",
    upl_statute="N.J.S.A. 2C:21-21 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="established_knowledge",
)
