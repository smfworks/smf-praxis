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
    MedicalProfile,
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

MEDICAL = MedicalProfile(
    state="FL",
    state_name="Florida",
    board_name="Florida Board of Medicine",
    board_url="https://flboardofmedicine.gov/",
    board_parent_agency="FL Department of Health, Division of Medical Quality Assurance",
    governing_statute="Ch. 458 (allopathic), Ch. 459 (osteopathic), Ch. 456 (general)",
    statute_url="https://www.flsenate.gov/Laws/Statutes/2024/Chapter458",
    license_cycle_years=2,
    license_renewal_note="biennial",
    imlc_member=True,
    imlc_citation="§458.3129, §456.4501",
    np_supervision_model="collaborative",
    np_supervision_citation="Ch. 464 (Nurse Practice Act); collaborative practice protocol required",
    corporate_practice_prohibited=True,
    corporate_practice_citation="FL prohibits corporate practice of medicine; corporate structure requires licensed physician control",
    upl_statute="Ch. 458.327 / 459.015 (unlicensed practice — third-degree felony)",
    record_retention_adult_years=5,
    record_retention_minor_years=7,
    record_retention_minor_rule="7 years from last visit, or until age 24 for minors (ESTABLISHED — verify pediatric extension)",
    record_retention_citation="§456.057",
    patient_access_days=15,
    patient_access_citation="§456.057 (records produced within a reasonable time; 15 days ESTABLISHED — verify)",
    telemedicine_requirement="registration",
    telemedicine_prior_in_person=False,
    telemedicine_citation="§456.47 — registration required for out-of-state providers; same standard of care",
    cross_state_practice_allowed=False,
    cross_state_citation="§456.47 (out-of-state registration for telehealth only)",
    written_consent_procedures="major procedures, experimental treatment (ESTABLISHED — verify)",
    telemedicine_consent_documented=True,
    advertising_filing_required=False,
    advertising_restrictions="false/misleading prohibited; testimonials regulated (ESTABLISHED — verify)",
    data_security_tier="breach_notification_only",
    data_security_citation="FL breach notification law §501.171",
    breach_notification_days=60,
    breach_notification_citation="§501.171 (60 days)",
    cme_required=True,
    cme_hours=40,
    cme_cycle_years=2,
    cme_mandatory_topics=("controlled_substance", "domestic_violence", "hiv_aids", "human_trafficking"),
    cme_topic_cycle_years=0,
    cme_citation="§458.331 — 40 hrs/biennium incl. CS, DV, HIV/AIDS, human trafficking",
    pmp_query_required=True,
    pmp_citation="§456.44 — PDMP query required before prescribing controlled substances",
    initial_opioid_rx_limit_days=7,
    initial_opioid_rx_citation="§458.3265 — 7-day initial opioid Rx limit for acute pain",
    mat_buprenorphine_permitted=True,
    mat_citation="DEA-registered prescribers (federal X-waiver eliminated Jan 2023)",
    minor_consent_services=("reproductive", "sti", "substance_use", "behavioral_health"),
    minor_parent_access_restricted=True,
    minor_consent_citation="§384.30 (STI), §384.25 (HIV) — minor may consent; ESTABLISHED for others — verify",
    confidence="primary_source",
)
