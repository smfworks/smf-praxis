"""West Virginia (WV) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 2 (primary_source).
Confidence: primary_source.
Notable: WV follows Daubert; firm registration required (§30-13-17);
WV explicitly REJECTS facsimile signatures (must be wet/original seal).
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile

FORENSIC = ForensicProfile(
    state="WV",
    state_name="West Virginia",
    board_name="WV State Board of Registration for Professional Engineers",
    board_url="https://wvpe.org",
    governing_statute="WV Code, Chapter 30, Article 13 (Professional Engineers)",
    statute_url="http://www.wvlegislature.gov/WVCODE/Code/30-13/master.html",
    firm_coa_required=True,
    firm_coa_citation="§30-13-17 (firm Certificate of Authorization)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="WV Electronic Transactions Act (WVC §39-5-1); note: WV rejects facsimile signatures — wet/original seal required",
    admissibility_standard="daubert",
    admissibility_citation="Wilt v. Buracker, 963 F.Supp. 1160 (S.D.W.Va. 1997); WV applies Daubert",
    pdh_hours=15,
    pdh_cycle_years=1,
    pdh_ethics_hours=2,
    pdh_citation="WV Board Rule 23CSR1 (continuing education; hours set by board)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="WV",
    state_name="West Virginia",
    bar_name="WV State Bar (integrated, mandatory)",
    bar_url="https://www.wvbar.org",
    governing_rules="WV Rules of Professional Conduct",
    rules_url="https://www.wvbar.org/rules-of-professional-conduct/",
    cle_required=True,
    cle_hours=24,
    cle_cycle_years=2,
    cle_ethics_hours=3,
    cle_citation="WV State Bar CLE rules (24 credits/2yr incl. 3 ethics)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="WVC §46A-2A-10 (breach notification; WV Consumer Credit Protection Act)",
    iolta_required=True,
    iolta_authority="WV State Bar IOLTA / WV Law Foundation",
    upl_statute="WVC §30-2-4 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="primary_source",
)
