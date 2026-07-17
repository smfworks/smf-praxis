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
    MedicalProfile,
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

MEDICAL = MedicalProfile(
    state="NY",
    state_name="New York",
    board_name="NY State Board for Medicine",
    board_url="https://www.op.nysed.gov/professions/medicine",
    board_parent_agency="NYSED Office of the Professions (Education Department, not Health)",
    governing_statute="NY Education Law Art. 131 (§§6520-6529)",
    statute_url="https://www.nysenate.gov/legislation/laws/EDN/A13 (Cloudflare-blocked — verify when web tools restored)",
    license_cycle_years=2,
    license_renewal_note="biennial",
    imlc_member=True,
    imlc_citation="NY Ed. Law — IMLC member (ESTABLISHED — verify section)",
    np_supervision_model="collaborative",
    np_supervision_citation="Ed. Law §6902+ — NP practice agreement (ESTABLISHED — verify)",
    corporate_practice_prohibited=True,
    corporate_practice_citation="NY Business Corp. Law §1107 (ESTABLISHED — verify)",
    upl_statute="Ed. Law §6512 (unauthorized practice — misdemeanor/felony) (ESTABLISHED — verify)",
    record_retention_adult_years=6,
    record_retention_minor_years=21,
    record_retention_minor_rule="6 years after last visit OR until age 21, whichever longer (ESTABLISHED — verify; PHL §18)",
    record_retention_citation="PHL §18 (ESTABLISHED — verify)",
    patient_access_days=10,
    patient_access_citation="PHL §18 — 10 business days (ESTABLISHED — verify)",
    telemedicine_requirement="no_prior_exam",
    telemedicine_prior_in_person=False,
    telemedicine_citation="NY PHL telehealth provisions (ESTABLISHED — verify)",
    cross_state_practice_allowed=False,
    cross_state_citation="Ed. Law §6522 (ESTABLISHED — verify)",
    written_consent_procedures="major procedures (PHL §2805-d defines lack of informed consent) (ESTABLISHED — verify)",
    telemedicine_consent_documented=True,
    advertising_filing_required=False,
    advertising_restrictions="false/misleading prohibited (ESTABLISHED — verify)",
    data_security_tier="shield_obligation",
    data_security_citation="GBL §899-bb (SHIELD Act reasonable safeguards) (ESTABLISHED — verify; statute text Cloudflare-blocked)",
    breach_notification_days=60,
    breach_notification_citation="GBL §899-aa (expeditiously; AG guidance 30-60 days) (ESTABLISHED — verify)",
    cme_required=True,
    cme_hours=100,
    cme_cycle_years=2,
    cme_mandatory_topics=("pain_management", "palliative_care", "end_of_life"),
    cme_topic_cycle_years=0,
    cme_citation="100/biennium incl. pain/palliative/end-of-life (ESTABLISHED — verify; PHL §3309-a may apply)",
    pmp_query_required=True,
    pmp_citation="PHL §3343 (I-STOP PMP) (ESTABLISHED — verify)",
    initial_opioid_rx_limit_days=7,
    initial_opioid_rx_citation="PHL §3309-a(9) (7-day initial opioid Rx) (ESTABLISHED — verify)",
    mat_buprenorphine_permitted=True,
    mat_citation="DEA-registered (ESTABLISHED — verify)",
    minor_consent_services=("reproductive", "sti", "substance_use", "behavioral_health"),
    minor_parent_access_restricted=True,
    minor_consent_citation="ESTABLISHED — verify",
    confidence="established_knowledge",
)
