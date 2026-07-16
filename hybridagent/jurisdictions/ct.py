"""Connecticut (CT) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 3 (established_knowledge — CT DPH site
reorganized, primary statute text not retrievable).
Confidence: established_knowledge. Re-verify when web tools are restored.

Notable: CT follows Daubert (State v. Porter); firm COA likely required (like
most states — not the divergence). CT has mandatory CLE (12 credits/yr).
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="CT",
    state_name="Connecticut",
    board_name="CT State Board of Examiners for Professional Engineers and Land Surveyors (Department of Public Health)",
    board_url="https://www.elicense.ct.gov",
    governing_statute="CT General Statutes, Chapter 393 (Professional Engineers and Land Surveyors), §§20-289-20-305",
    statute_url="https://www.cga.ct.gov/",
    firm_coa_required=True,
    firm_coa_citation="C.G.S. §20-298 (firm Certificate of Authorization — verify exact section)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="CT Uniform Electronic Transactions Act (C.G.S. §1-276)",
    admissibility_standard="daubert",
    admissibility_citation="State v. Porter, 241 Conn. 57, 698 A.2d 739 (1997) (CT adopted Daubert)",
    pdh_hours=24,
    pdh_cycle_years=2,
    pdh_ethics_hours=0,
    pdh_citation="CT DPH continuing education rules (24 PDH/biennium — verify exact hours)",
    forensic_specific_rules="None",
    confidence="established_knowledge",
)


LEGAL = LegalProfile(
    state="CT",
    state_name="Connecticut",
    bar_name="CT Bar Association (voluntary); CT Statewide Grievance Committee",
    bar_url="https://www.ctbar.org",
    governing_rules="CT Rules of Professional Conduct",
    rules_url="https://www.jud.ct.gov/sgc/",
    cle_required=True,
    cle_hours=12,
    cle_cycle_years=1,
    cle_ethics_hours=2,
    cle_citation="CT MCLE rules (12 credits/yr incl. 2 ethics — self-certification model)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="C.G.S. §42-471 (breach notification; CT general breach law)",
    iolta_required=True,
    iolta_authority="CT Bar Foundation IOLTA program",
    upl_statute="C.G.S. §51-88 (unauthorized practice of law — misdemeanor)",
    mdp_prohibited=True,
    confidence="established_knowledge",
)
