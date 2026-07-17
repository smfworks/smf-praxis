"""South Carolina (SC) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 1 — primary-source (full statute text).
Confidence: primary_source.

Notable:
- SC is the **outlier on admissibility**: it uses its own Painter/Council
  standard (from State v. Council), not Daubert or Frye. The only state in the
  13 with a non-Daubert/non-Frye standard.
- SC explicitly includes "expert technical testimony" in its statutory
  definition of engineering practice (S.C. Code §40-22-20(25)) — the only
  state to mention expert testimony in the engineering statute.
"""
from __future__ import annotations

from . import (
    ForensicProfile,
    LegalProfile,
    MedicalProfile,
)

FORENSIC = ForensicProfile(
    state="SC",
    state_name="South Carolina",
    board_name="SC State Board of Registration for Professional Engineers and Land Surveyors",
    board_url="https://www.llr.sc.gov/eng/",
    governing_statute="SC Code of Laws, Title 40, Chapter 22 (Professional Engineers and Land Surveyors)",
    statute_url="https://www.scstatehouse.gov/code/t40c022.php",
    firm_coa_required=True,
    firm_coa_citation="§40-22-250 (Certificate of Authorization for firms)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="SC Uniform Electronic Transactions Act (SC Code §26-6-10 et seq.)",
    admissibility_standard="painter",
    admissibility_citation="State v. Council, 335 S.C. 1, 515 S.E.2d 508 (1999) (SC's own standard — reliability + general acceptance hybrid)",
    pdh_hours=30,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="SC Board Regulation 49-21 (continuing education; 30 PDH/biennium incl. 2 ethics)",
    forensic_specific_rules="§40-22-20(25) explicitly includes 'expert technical testimony' in the definition of engineering practice (only state to do so)",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="SC",
    state_name="South Carolina",
    bar_name="SC Bar (unified, mandatory)",
    bar_url="https://www.scbar.org",
    governing_rules="SC Rules of Professional Conduct",
    rules_url="https://www.scbar.org/ForLawyers/EthicsAdvisoryOpinions/",
    cle_required=True,
    cle_hours=14,
    cle_cycle_years=1,
    cle_ethics_hours=2,
    cle_citation="SC CLE Commission rules (14 credits/yr incl. 2 ethics + 2 substance abuse/3yr)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="SC Data Breach Notification Act (§39-1-90)",
    iolta_required=True,
    iolta_authority="SC IOLTA program / SC Bar Foundation",
    upl_statute="SC Code §40-5-310 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="primary_source",
)

MEDICAL = MedicalProfile(
    state="SC",
    state_name="South Carolina",
    board_name="State Board of Medical Examiners",
    board_url="https://www.llr.sc.gov/med/",
    board_parent_agency="SC Dept. of Labor, Licensing & Regulation (LLR)",
    governing_statute="S.C. Code Title 40, Ch. 47",
    statute_url="https://www.scstatehouse.gov/code/t40c047.php",
    license_cycle_years=2,
    license_renewal_note="biennial (even years)",
    imlc_member=True,
    imlc_citation="S.C. Code — IMLC member (ESTABLISHED — verify section)",
    np_supervision_model="supervision",
    np_supervision_citation="§40-47-30s — NP scope limited (ESTABLISHED — verify exact section)",
    corporate_practice_prohibited=True,
    corporate_practice_citation="§40-47-40 (ESTABLISHED — verify)",
    upl_statute="§40-47-90 (unauthorized practice — misdemeanor) (ESTABLISHED — verify)",
    record_retention_adult_years=7,
    record_retention_minor_years=21,
    record_retention_minor_rule="until age 21 (ESTABLISHED — verify)",
    record_retention_citation="§40-47-110(B)(17) — retention referenced in disciplinary grounds",
    patient_access_days=0,
    patient_access_citation="ESTABLISHED — verify (HIPAA floor)",
    telemedicine_requirement="no_prior_exam",
    telemedicine_prior_in_person=False,
    telemedicine_citation="§40-47-37 — SC license required; same standard of care",
    cross_state_practice_allowed=False,
    cross_state_citation="§40-47-37",
    written_consent_procedures="ESTABLISHED — verify",
    telemedicine_consent_documented=True,
    advertising_filing_required=False,
    advertising_restrictions="§40-47-110 — false/misleading prohibited (ESTABLISHED — verify)",
    data_security_tier="breach_notification_only",
    data_security_citation="SC breach notification law §39-1-90 (ESTABLISHED — verify)",
    breach_notification_days=60,
    breach_notification_citation="ESTABLISHED — verify (likely 90 days SC)",
    cme_required=True,
    cme_hours=40,
    cme_cycle_years=2,
    cme_mandatory_topics=(),
    cme_topic_cycle_years=0,
    cme_citation="40 hrs/biennium Category I (ESTABLISHED — verify)",
    pmp_query_required=True,
    pmp_citation="§40-47-113; SC PMP (Article 15, Ch. 53, Title 44)",
    initial_opioid_rx_limit_days=0,
    initial_opioid_rx_citation="ESTABLISHED — verify",
    mat_buprenorphine_permitted=True,
    mat_citation="DEA-registered (ESTABLISHED — verify)",
    minor_consent_services=("reproductive", "sti", "substance_use", "behavioral_health"),
    minor_parent_access_restricted=True,
    minor_consent_citation="SC Code §44-29-140 (STI minor consent — verify)",
    confidence="primary_source",
)
