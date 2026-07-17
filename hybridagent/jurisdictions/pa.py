"""Pennsylvania (PA) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 2 (WV/MD/PA/OH/NJ).
Confidence: forensic = primary_source; legal = established_knowledge (PA bar
site retrieval was partial). PDH count and admissibility are well-established.

Notable: PA uses the **Frye** standard for expert testimony (not Daubert) —
one of only two Frye jurisdictions in the 13 (NY is the other). This is the
most significant forensic divergence for a firm using Praxis in PA courts.
"""
from __future__ import annotations

from . import (
    ForensicProfile,
    LegalProfile,
    MedicalProfile,
)

FORENSIC = ForensicProfile(
    state="PA",
    state_name="Pennsylvania",
    board_name="Pennsylvania State Registration Board for Professional Engineers, Land Surveyors and Geologists",
    board_url="https://www.dos.pa.gov/ProfessionalLicensing/BoardsCommissions/EngineersLandSurveyorsandGeologists",
    governing_statute="PE Act, 63 P.S. §§ 130-312-318 (Pennsylvania Engineer, Land Surveyor and Geologist Registration Law)",
    statute_url="https://www.legis.state.pa.us/WU01/LI/LI/US/PDF/1992/0/0091..PDF",
    firm_coa_required=True,
    firm_coa_citation="63 P.S. § 130.71 (Certificate of Approval of Name)",
    firm_coa_fee="",
    electronic_seal_authorized=True,
    electronic_seal_citation="37 PS § 37.60 (digital seals: unique, verifiable, sole control, document-linked)",
    admissibility_standard="frye",
    admissibility_citation="Commonwealth v. Minerd, 819 A.2d 581 (Pa. Super. 2003); PA applies Frye (general acceptance)",
    pdh_hours=24,
    pdh_cycle_years=2,
    pdh_ethics_hours=2,
    pdh_citation="49 PA Code § 47.31 (continuing education)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="PA",
    state_name="Pennsylvania",
    bar_name="Pennsylvania Bar Association (voluntary); Disciplinary Board of PA",
    bar_url="https://www.pabar.org",
    governing_rules="PA Rules of Professional Conduct",
    rules_url="https://www.padisciplinaryboard.org/for-lawyers/rules-of-professional-conduct",
    cle_required=True,
    cle_hours=12,
    cle_cycle_years=1,
    cle_ethics_hours=1,
    cle_citation="PA CLE Board rules (continuing legal education)",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="breach_notification_only",
    data_security_citation="73 P.S. § 2301 (PA Breach of Personal Information Notification Act)",
    iolta_required=True,
    iolta_authority="PA IOLTA Board / PA Lawyers Fund for Client Security",
    upl_statute="42 Pa.C.S. § 2521 (unauthorized practice of law)",
    mdp_prohibited=True,
    confidence="established_knowledge",
)

MEDICAL = MedicalProfile(
    state="PA",
    state_name="Pennsylvania",
    board_name="PA State Board of Medicine",
    board_url="https://www.dos.pa.gov/ProfessionalLicensing/BoardsCommissions/Medicine",
    board_parent_agency="PA Department of State, Bureau of Professional and Occupational Affairs",
    governing_statute="63 P.S. §§422.1-422.53; 49 Pa. Code Ch. 16-18",
    statute_url="https://www.pacodeandbulletin.gov/",
    license_cycle_years=2,
    license_renewal_note="biennial (expires Dec 31, even years)",
    imlc_member=False,
    imlc_citation="PA is NOT an IMLC member state (confirmed batch 2) — no Compact expedited licensure",
    np_supervision_model="collaborative",
    np_supervision_citation="49 Pa. Code §21.11+ — collaborative practice agreement (ESTABLISHED — verify)",
    corporate_practice_prohibited=True,
    corporate_practice_citation="PA corporate practice doctrine (ESTABLISHED — verify)",
    upl_statute="63 P.S. §422.40+ (unlicensed practice — misdemeanor/felony) (ESTABLISHED — verify)",
    record_retention_adult_years=7,
    record_retention_minor_years=21,
    record_retention_minor_rule="7 years adult; until age 21 for minors (ESTABLISHED — verify)",
    record_retention_citation="49 Pa. Code §16.91+ (ESTABLISHED — verify)",
    patient_access_days=30,
    patient_access_citation="ESTABLISHED — verify (30 days; HIPAA floor)",
    telemedicine_requirement="no_prior_exam",
    telemedicine_prior_in_person=False,
    telemedicine_citation="ESTABLISHED — verify (PA telemedicine rules; board guidance)",
    cross_state_practice_allowed=False,
    cross_state_citation="ESTABLISHED — verify (PA non-IMLC — stricter)",
    written_consent_procedures="ESTABLISHED — verify",
    telemedicine_consent_documented=True,
    advertising_filing_required=False,
    advertising_restrictions="ESTABLISHED — verify",
    data_security_tier="breach_notification_only",
    data_security_citation="PA breach notification 73 PS §2301+ (ESTABLISHED — verify)",
    breach_notification_days=60,
    breach_notification_citation="ESTABLISHED — verify (PA likely 60 days)",
    cme_required=True,
    cme_hours=100,
    cme_cycle_years=2,
    cme_mandatory_topics=("opioid_education",),
    cme_topic_cycle_years=0,
    cme_citation="49 Pa. Code §16.107 — 100 CME/biennium (20 Cat 1); opioid education as licensure prerequisite",
    pmp_query_required=True,
    pmp_citation="35 Pa.C.S. §1206.2+ (PDMP) (ESTABLISHED — verify)",
    initial_opioid_rx_limit_days=7,
    initial_opioid_rx_citation="ESTABLISHED — verify (PA may have 7-day limit)",
    mat_buprenorphine_permitted=True,
    mat_citation="DEA-registered (ESTABLISHED — verify)",
    minor_consent_services=("reproductive", "sti", "substance_use", "behavioral_health"),
    minor_parent_access_restricted=True,
    minor_consent_citation="ESTABLISHED — verify",
    confidence="primary_source",
)
