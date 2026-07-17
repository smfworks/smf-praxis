"""Connecticut (CT) — forensic engineering + law firm regulatory profiles.

Sources: 13-state-gap-analysis.md batch 3 (established_knowledge — CT DPH site
reorganized, primary statute text not retrievable).
Confidence: established_knowledge. Re-verify when web tools are restored.

Notable: CT follows Daubert (State v. Porter); firm COA likely required (like
most states — not the divergence). CT has mandatory CLE (12 credits/yr).
"""
from __future__ import annotations

from . import ForensicProfile, LegalProfile, MedicalProfile

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

MEDICAL = MedicalProfile(
    state="CT",
    state_name="Connecticut",
    board_name="CT Medical Examining Board (21 members: 13 physicians, 1 PA, 7 public)",
    board_url="https://portal.ct.gov/dph/practitioner-licensing--investigations/plis/medical-examining-board",
    board_parent_agency="CT Department of Public Health (DPH), Practitioner Licensing & Investigations Section (PLIS)",
    governing_statute="CGS Ch. 370 (§§20-8a et seq.)",
    statute_url="https://www.cga.ct.gov/current/pub/chap_370.htm (cga.ct.gov unreachable — web.archive.org cache used)",
    license_cycle_years=1,
    license_renewal_note="annual in birth month; 90-day grace post-expiration (§19a-88(e)(4)f)",
    imlc_member=True,
    imlc_citation="CGS — IMLC member (ESTABLISHED — verify section)",
    np_supervision_model="collaborative",
    np_supervision_citation="§20-12d — PA delegation agreement; APRN (separate) (ESTABLISHED — verify APRN)",
    corporate_practice_prohibited=True,
    corporate_practice_citation="CT Business Corp. Act (ESTABLISHED — verify)",
    upl_statute="§20-9 (unauthorized practice — §20-13c disciplinary grounds) (verified)",
    record_retention_adult_years=7,
    record_retention_minor_years=21,
    record_retention_minor_rule="7 years adult; until age 21 for minors (ESTABLISHED — verify)",
    record_retention_citation="CME records kept 6 years (§20-10b verified); general medical-record retention ESTABLISHED — verify",
    patient_access_days=30,
    patient_access_citation="ESTABLISHED — verify (30 days; HIPAA floor)",
    telemedicine_requirement="no_prior_exam",
    telemedicine_prior_in_person=False,
    telemedicine_citation="ESTABLISHED — verify (CT telehealth parity)",
    cross_state_practice_allowed=False,
    cross_state_citation="§20-9 (license required to practice in CT)",
    written_consent_procedures="ESTABLISHED — verify",
    telemedicine_consent_documented=True,
    advertising_filing_required=False,
    advertising_restrictions="ESTABLISHED — verify",
    data_security_tier="breach_notification_only",
    data_security_citation="CGS §36a-701b (ESTABLISHED — verify)",
    breach_notification_days=60,
    breach_notification_citation="ESTABLISHED — verify (likely 60 days CT)",
    cme_required=True,
    cme_hours=50,
    cme_cycle_years=2,
    cme_mandatory_topics=("infectious_diseases", "risk_management", "sexual_assault", "domestic_violence", "cultural_competency", "behavioral_health"),
    cme_topic_cycle_years=6,
    cme_citation="§20-10b — 50 contact hours / 24-month period; 6-year mandatory topic cycles incl. cultural competency (systemic racism, transgender care), behavioral health (suicide, cognitive, veterans)",
    pmp_query_required=True,
    pmp_citation="ESTABLISHED — verify (CT PMP is CPMRS)",
    initial_opioid_rx_limit_days=0,
    initial_opioid_rx_citation="ESTABLISHED — verify",
    mat_buprenorphine_permitted=True,
    mat_citation="DEA-registered (ESTABLISHED — verify)",
    minor_consent_services=("reproductive", "sti", "substance_use", "behavioral_health"),
    minor_parent_access_restricted=True,
    minor_consent_citation="ESTABLISHED — verify",
    confidence="primary_source",
)
