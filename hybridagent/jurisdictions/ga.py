"""Georgia (GA) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 1 (primary_source, partial — some GA
bar/SOS pages were Cloudflare-blocked).
Confidence: forensic = primary_source; legal = established_knowledge.
Notable: GA follows Daubert; firm COA required (O.C.G.A. §43-15-13).
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="GA",
    state_name="Georgia",
    board_name="Georgia State Board of Registration for Professional Engineers and Land Surveyors",
    board_url="https://sos.ga.gov/georgia-consumer-protection/boards/registration-professional-engineers-and-land-surveyors",
    governing_statute="O.C.G.A. Chapter 15 of Title 43 (Professional Engineers and Land Surveyors)",
    statute_url="https://law.georgia.gov/paraphrase/?title=43&chapter=15",
    firm_coa_required=True,
    firm_coa_citation="O.C.G.A. §43-15-13 (Certificate of Authorization for firms)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="Georgia Electronic Records and Signatures Act (O.C.G.A. §10-12-1 et seq.)",
    admissibility_standard="daubert",
    admissibility_citation="GA follows Daubert (general acceptance of reliable scientific evidence)",
    pdh_hours=30,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="Ga. Comp. R. & Regs. r. 180-7 (continuing education; 30 PDH/biennium incl. 2 ethics)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="GA",
    state_name="Georgia",
    bar_name="State Bar of Georgia (unified, mandatory)",
    bar_url="https://www.gabar.org",
    governing_rules="Georgia Rules of Professional Conduct",
    rules_url="https://www.gabar.org/forthepublic/lawyerregulation/georgiarulesofprofessionalconduct.cfm",
    cle_required=True,
    cle_hours=12,
    cle_cycle_years=1,
    cle_ethics_hours=1,
    cle_citation="GA CLE rules (12 credits/yr incl. ethics + trial practice)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="O.C.G.A. §10-1-912 (GA Identity Theft Protection Act; breach notification)",
    iolta_required=True,
    iolta_authority="Georgia Bar Foundation / GA IOLTA program",
    upl_statute="O.C.G.A. §15-19-9 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="established_knowledge",
)
