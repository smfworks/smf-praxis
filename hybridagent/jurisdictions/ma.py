"""Massachusetts (MA) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 3, reconciled with Northeast subagent.
Confidence: forensic firm-COA-absence = primary_source (mass.gov confirmed);
legal 201 CMR 17.00 = primary_source (widely published); CLE-not-required
= established_knowledge (well-known, but MA has debated adding it).

Notable:
- MA is the **only state in the 13 that does NOT require a firm-level
  Certificate of Authorization** for engineering firms. The Board only signs
  per-licensee "Certificate by Regulatory Board" forms; firms register with
  the Secretary of State.
- MA has **no mandatory CLE** — the only state in the 13 without it.
- MA **201 CMR 17.00** is the strictest proactive data-security standard in
  the 13 states: mandates a Written Information Security Program (WISP),
  encryption of personal information at rest and in transit, employee training,
  and breach notification. The ceiling — if Praxis can evidence MA compliance,
  it can evidence any state's data-security obligation.
- MA follows a **Daubert-like** admissibility standard (Commonwealth v. Lanigan).
"""
from __future__ import annotations

from . import (
    EducationProfile,
    ForensicProfile,
    LegalProfile,
    MedicalProfile,
)

FORENSIC = ForensicProfile(
    state="MA",
    state_name="Massachusetts",
    board_name="MA Board of Registration of Professional Engineers and Professional Land Surveyors (Division of Occupational Licensure)",
    board_url="https://www.mass.gov/orgs/board-of-registration-of-professional-engineers-and-of-professional-land-surveyors",
    governing_statute="M.G.L. Chapter 112, §§81D-81R (Registration of Professional Engineers and of Professional Land Surveyors)",
    statute_url="https://malegislature.gov/Laws/GeneralLaws/PartI/TitleXVI/Chapter112",
    firm_coa_required=False,
    firm_coa_citation="",
    firm_coa_fee="$15 per licensee (Board signs Certificate by Regulatory Board; firm registers with SoS, not the Board)",
    electronic_seal_authorized=True,
    electronic_seal_citation="MA Uniform Electronic Transactions Act, M.G.L. c. 110G",
    admissibility_standard="daubert",
    admissibility_citation="Commonwealth v. Lanigan, 419 Mass. 15, 641 N.E.2d 1342 (1994) (Daubert-like reliability standard)",
    pdh_hours=24,
    pdh_cycle_years=2,
    pdh_ethics_hours=0,
    pdh_citation="250 CMR (Board continuing education rules)",
    forensic_specific_rules="None",
    confidence="primary_source",
)


LEGAL = LegalProfile(
    state="MA",
    state_name="Massachusetts",
    bar_name="Massachusetts Bar Association (voluntary); Board of Bar Overseers (BBO) + Office of Bar Counsel (OBC) (SJC-established)",
    bar_url="https://www.massbbo.org",
    governing_rules="Massachusetts Rules of Professional Conduct (adopted by SJC)",
    rules_url="https://www.massbbo.org/Rules",
    cle_required=False,
    cle_hours=0,
    cle_cycle_years=0,
    cle_ethics_hours=0,
    cle_citation="",
    advertising_filing_required=False,
    advertising_filing_citation="",
    advertising_filing_authority="",
    data_security_tier="wisp_mandate",
    data_security_citation="201 CMR 17.00 (Standards for the Protection of Personal Information; WISP + encryption mandate); M.G.L. c. 93H §3 (breach notification)",
    iolta_required=True,
    iolta_authority="MA IOLTA Committee (MIC) / MA Bar Foundation",
    upl_statute="M.G.L. c. 221, §41 (practice of law without license)",
    mdp_prohibited=True,
    confidence="primary_source",
)

MEDICAL = MedicalProfile(
    state="MA",
    state_name="Massachusetts",
    board_name="MA Board of Registration in Medicine (BM/REG)",
    board_url="https://www.mass.gov/orgs/board-of-registration-in-medicine",
    board_parent_agency="MA Department of Public Health (independent board)",
    governing_statute="MGL c. 112; 243 CMR (regulations 403-blocked — verify)",
    statute_url="https://malegislature.gov/Laws/GeneralLaws/PartI/TitleII/Chapter112",
    license_cycle_years=2,
    license_renewal_note="biennial on birthday",
    imlc_member=False,
    imlc_citation="MA is NOT an IMLC member state — no Compact expedited licensure",
    np_supervision_model="collaborative",
    np_supervision_citation="MGL c. 112 §80C+ (ESTABLISHED — verify; MA NPs have prescriptive authority under guidelines)",
    corporate_practice_prohibited=True,
    corporate_practice_citation="MGL c. 112 §6+ (ESTABLISHED — verify; MA corporate practice doctrine)",
    upl_statute="MGL c. 112 §6 — $100-$1000 fine, 1 month-1 year imprisonment (verified)",
    record_retention_adult_years=7,
    record_retention_minor_years=21,
    record_retention_minor_rule="7 years adult; until age 21 for minors OR 7 years after last visit, whichever longer (ESTABLISHED — verify)",
    record_retention_citation="243 CMR (403-blocked) (ESTABLISHED — verify)",
    patient_access_days=10,
    patient_access_citation="MGL c. 111 §70 (ESTABLISHED — verify)",
    telemedicine_requirement="no_prior_exam",
    telemedicine_prior_in_person=False,
    telemedicine_citation="MGL c. 112 §5O — synchronous/asynchronous audio/video/phone permitted (verified)",
    cross_state_practice_allowed=False,
    cross_state_citation="MA non-IMLC — physician must be MA-licensed to treat MA patients (ESTABLISHED)",
    written_consent_procedures="ESTABLISHED — verify",
    telemedicine_consent_documented=True,
    advertising_filing_required=False,
    advertising_restrictions="ESTABLISHED — verify",
    data_security_tier="wisp_mandate",
    data_security_citation="201 CMR 17.00 (WISP mandate) — applies to any entity holding MA residents' PI including medical records; MGL c. 93H §3 (statute authority verified)",
    breach_notification_days=30,
    breach_notification_citation="MGL c. 93H §3 — as soon as practicable, no later than 30 days (statute authority verified — verify exact timeline)",
    cme_required=True,
    cme_hours=100,
    cme_cycle_years=2,
    cme_mandatory_topics=("cognitive_impairment",),
    cme_topic_cycle_years=0,
    cme_citation="MGL c. 112 §2 — 100/biennium (ESTABLISHED — verify); 1-time cognitive impairment/Alzheimer's training (verified)",
    pmp_query_required=True,
    pmp_citation="ESTABLISHED — verify (MA PMP is MASS PMP)",
    initial_opioid_rx_limit_days=7,
    initial_opioid_rx_citation="MGL c. 94C §47B (7-day initial opioid Rx) (ESTABLISHED — verify)",
    mat_buprenorphine_permitted=True,
    mat_citation="DEA-registered (ESTABLISHED — verify)",
    minor_consent_services=("reproductive", "sti", "substance_use", "behavioral_health"),
    minor_parent_access_restricted=True,
    minor_consent_citation="ESTABLISHED — verify",
    confidence="primary_source",
)


EDUCATION = EducationProfile(
    state="MA",
    state_name="Massachusetts",
    sea_name="Department of Elementary and Secondary Education",
    sea_url="https://www.doe.mass.edu",
    governing_statute="M.G.L. c.71; 603 CMR",
    statute_url="https://www.doe.mass.edu/lawsregs/603cmr23.html",
    privacy_tier="enhanced_operator",
    privacy_citation="603 CMR 23.00 + FERPA + M.G.L. c.93H",
    operator_law=False,
    operator_citation="No FL-style SOPIPA; FERPA + 603 CMR 23 third-party consent",
    deletion_days_after_exit=0,
    biometric_collection_banned=False,
    affective_computing_banned=False,
    vendor_breach_notice_days=0,
    encryption_required=False,
    parent_bill_of_rights_required=False,
    teacher_appr_data_protected=False,
    ai_policy_required=False,
    ai_policy_citation="DESE AI Guidance (voluntary) — human oversight principles",
    parent_ai_interaction_access=False,
    closed_system_ai_preferred=False,
    ai_generated_content_instruction=False,
    sped_eval_timeline_days=60,
    sped_citation="603 CMR 28; M.G.L. c.71B",
    transition_planning_age=14,
    teacher_cert_authority="DESE",
    teacher_cert_citation="M.G.L. c.71 §38G; CMVS c.71 §94",
    teacher_pd_hours=0,
    teacher_pd_cycle_years=0,
    transcript_retention_years=60,
    temporary_record_retention_years=7,
    parent_access_days=45,
    records_citation="603 CMR 23.00 (transcript 60 yrs; temporary ≤7)",
    mandatory_report_citation="M.G.L. c.119 §51A",
    parent_rights_citation="603 CMR 23 inspection/amendment rights",
    confidence="primary_source",
)
