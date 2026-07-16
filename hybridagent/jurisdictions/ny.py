"""New York (NY) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 3 (NY/CT/MA), reconciled with the
Northeast subagent's primary-source retrieval.
Confidence: forensic firm-COA = primary_source (NYSED §7210 confirmed);
legal advertising = primary_source (22 NYCRR Part 1200, widely published);
PDH count = established_knowledge (NYSED article index shows §7211 mandatory CE).

Notable:
- NY uses the **Frye** standard for expert testimony (not Daubert) — one of
  only two Frye jurisdictions in the 13 (PA is the other).
- NY has the **strictest attorney-advertising rules in the nation** — 22 NYCRR
  Part 1200 requires filing with the Appellate Division. Only FL also requires filing.
- NY **SHIELD Act** adds an affirmative data-security obligation (reasonable
  administrative, technical, and physical safeguards) beyond breach notification.
"""
from __future__ import annotations

from . import (
    ForensicProfile,
    LegalProfile,
)

FORENSIC = ForensicProfile(
    state="NY",
    state_name="New York",
    board_name="NY State Board for Engineering, Land Surveying and Geology (NY State Education Department, Office of the Professions)",
    board_url="https://www.op.nysed.gov/professions/engineering",
    governing_statute="NY Education Law, Article 145 (Professional Engineering and Land Surveying), §§7200-7211",
    statute_url="https://www.nysenate.gov/legislation/laws/EDN/A145",
    firm_coa_required=True,
    firm_coa_citation="Education Law §7210 (Certificate of Authorization)",
    firm_coa_fee="$90 (plus $20 for certified copy filing)",
    electronic_seal_authorized=True,
    electronic_seal_citation="Electronic Signatures and Records Act (ESRA), State Technology Law §304",
    admissibility_standard="frye",
    admissibility_citation="People v. Wesley, 83 N.Y.2d 417 (1994); People v. LeGrand; NY follows Frye (general acceptance)",
    pdh_hours=36,
    pdh_cycle_years=3,
    pdh_ethics_hours=1,
    pdh_citation="Education Law §7211 (mandatory continuing education for PEs)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="NY",
    state_name="New York",
    bar_name="New York State Bar Association (voluntary); NY court system (Appellate Divisions admit/discipline)",
    bar_url="https://www.nycourts.gov/attorneys/",
    governing_rules="NY Rules of Professional Conduct (22 NYCRR Part 1200)",
    rules_url="https://www.nycourts.gov/rules/joint/appellate/rules_part1200.shtml",
    cle_required=True,
    cle_hours=24,
    cle_cycle_years=2,
    cle_ethics_hours=4,
    cle_citation="22 NYCRR Part 1500 (CLE; includes 4 ethics, 1 diversity/inclusion, 1 cybersecurity)",
    advertising_filing_required=True,
    advertising_filing_citation="22 NYCRR Part 1200, Rules 7.1-7.5",
    advertising_filing_authority="Attorney Advertising Registration Unit, Appellate Division (must file solicitation materials; 'Attorney Advertising' label + disclaimers required)",
    data_security_tier="shield_obligation",
    data_security_citation="NY SHIELD Act, General Business Law §899-aa (breach notification) and §899-bb (reasonable safeguards duty)",
    iolta_required=True,
    iolta_authority="NY IOLTA program / NY Lawyers' Fund for Client Protection",
    upl_statute="NY Judiciary Law §478 (UPL misdemeanor) and §484 (enforcement)",
    mdp_prohibited=True,
    confidence="primary_source",
)