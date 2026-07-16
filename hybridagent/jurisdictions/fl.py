"""Florida (FL) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 1 (FL/GA/SC/TN/VA) — primary-source.
Confidence: primary_source (full statute text retrieved).

Notable:
- FL is one of only two states requiring attorney-advertising filing (with The
  Florida Bar). NY is the other.
- FL has the most heavily regulated law-firm environment in the 13: mandatory ad
  filing, contingency fee caps, explicit technology-competence CLE requirement.
- FL follows **Daubert** for expert testimony (as of 2013, amended from Frye).
"""
from __future__ import annotations

from . import (
    ForensicProfile,
    LegalProfile,
)

FORENSIC = ForensicProfile(
    state="FL",
    state_name="Florida",
    board_name="Florida Board of Professional Engineers",
    board_url="https://fbpe.org",
    governing_statute="Florida Statutes Chapter 471 (Engineering)",
    statute_url="http://www.leg.state.fl.us/statutes/index.cfm?App_mode=Display_Statute&URL=0400-0499/0471/0471.html",
    firm_coa_required=True,
    firm_coa_citation="§471.023 (firm qualification; Certificate of Authorization)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="Florida Electronic Signatures and Records Act (FESRA), §668.50",
    admissibility_standard="daubert",
    admissibility_citation="§90.702 (FL Evidence Code; adopted Daubert in 2013, replacing Frye)",
    pdh_hours=18,
    pdh_cycle_years=2,
    pdh_ethics_hours=1,
    pdh_citation="§471.019 (continuing education; 18 PDH/biennium incl. 1 ethics + 1 area of practice)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="FL",
    state_name="Florida",
    bar_name="The Florida Bar (integrated, mandatory)",
    bar_url="https://www.floridabar.org",
    governing_rules="Florida Rules of Professional Conduct",
    rules_url="https://www.floridabar.org/ethics/ethics-rules/",
    cle_required=True,
    cle_hours=33,
    cle_cycle_years=3,
    cle_ethics_hours=5,
    cle_citation="Florida Bar CLE rules (33 credits/3yr incl. 5 ethics + technology CLE requirement)",
    advertising_filing_required=True,
    advertising_filing_citation="Florida Rules of Professional Conduct, Rules 4-7.1 to 4-7.5",
    advertising_filing_authority="The Florida Bar (filing required; content + format restrictions; disclaimers)",
    data_security_tier="breach_notification_only",
    data_security_citation="Florida Information Protection Act (§501.171); breach notification",
    iolta_required=True,
    iolta_authority="The Florida Bar Foundation / FL IOLTA program",
    upl_statute="Chapter 454, Florida Statutes (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="primary_source",
)