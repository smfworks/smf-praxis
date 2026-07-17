"""Per-jurisdiction regulatory profiles for professional verticals.

Gap 1 of the 13-state Forensic Engineering / Law Firm vertical build-out
(see workspace/research/13-state-gap-analysis.md). This is the foundation:
a structured catalog of what each state requires, so downstream features
(Gap 2 NY ad-filing, Gap 3 MA WISP attestation, Gap 4 CE tracking, etc.) can
load state-specific rules instead of hardcoding them.

Design:
- One module per state (``jurisdictions/fl.py``, ``ga.py``, ... ``ma.py``).
  Each exposes ``FORENSIC`` and ``LEGAL`` profile constants. Keeping the
  state-level research in one file per state means a re-verification pass
  touches one file, not 26.
- ``get_forensic_profile(state)`` / ``get_legal_profile(state)`` import the
  state module on demand and return the profile, or ``None`` if the state
  isn't in the registry.
- Profiles carry a ``confidence`` field so downstream features can flag
  unverified data rather than silently relying on it (per the gap analysis
  verification needs — CT firm-COA and several PDH/CLE counts are still
  established-knowledge, not primary-source-verified).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

# Admissibility standards for expert testimony
Admissibility = Literal["daubert", "frye", "painter", "other"]

# Data-security standard tier
DataSecurityTier = Literal[
    "wisp_mandate",        # MA 201 CMR 17.00 — proactive WISP + encryption
    "shield_obligation",   # NY SHIELD Act — affirmative security duty
    "breach_notification_only",  # most states — notify on breach, no proactive standard
]


@dataclass(frozen=True)
class ForensicProfile:
    """Per-state forensic-engineering regulatory facts.

    No state regulates "forensic engineering" separately; these are the
    general PE-licensing facts that apply to forensic work in that state.
    """
    state: str                     # two-letter code, e.g. "PA"
    state_name: str
    board_name: str
    board_url: str
    governing_statute: str         # e.g. "PE Act §471.023"
    statute_url: str               # primary source
    firm_coa_required: bool        # firm Certificate of Authorization
    firm_coa_citation: str          # statute, or "" if not required
    firm_coa_fee: str              # e.g. "$90", or "" if not required
    electronic_seal_authorized: bool
    electronic_seal_citation: str
    admissibility_standard: Admissibility   # Daubert / Frye / Painter / other
    admissibility_citation: str            # case or statute
    pdh_hours: int                         # per cycle
    pdh_cycle_years: int                   # 1=annual, 2=biennium, 3=triennium
    pdh_ethics_hours: int
    pdh_citation: str
    forensic_specific_rules: str          # "None" for all 13 states (no state regulates separately)
    confidence: str = "primary_source"    # primary_source | established_knowledge | mixed


@dataclass(frozen=True)
class LegalProfile:
    """Per-state law-firm regulatory facts."""
    state: str
    state_name: str
    bar_name: str
    bar_url: str
    governing_rules: str          # e.g. "22 NYCRR Part 1200"
    rules_url: str                # primary source
    cle_required: bool           # MA is the only False
    cle_hours: int               # per cycle (0 if not required)
    cle_cycle_years: int          # 1=annual, 2=biennium, 3=triennium
    cle_ethics_hours: int
    cle_citation: str
    advertising_filing_required: bool   # NY + FL only
    advertising_filing_citation: str
    advertising_filing_authority: str  # where to file, e.g. "NY Appellate Division"
    data_security_tier: DataSecurityTier
    data_security_citation: str
    iolta_required: bool          # all 13 True
    iolta_authority: str
    upl_statute: str              # unauthorized practice of law citation
    mdp_prohibited: bool          # non-lawyer ownership (all 13 True)
    confidence: str = "primary_source"


# NP/PA supervision model
SupervisionModel = Literal[
    "independent",          # NP may practice + prescribe without physician supervision
    "collaborative",        # NP/PA practices under a collaborative practice agreement
    "supervision",          # NP/PA requires physician supervision (delegation)
]

# Telemedicine prior-relationship requirement
TelemedicineRequirement = Literal[
    "no_prior_exam",        # no in-person exam required before a tele-visit
    "prior_exam_required",  # an established relationship / prior in-person exam required
    "registration",         # out-of-state providers must register (e.g. FL §456.47)
]


@dataclass(frozen=True)
class MedicalProfile:
    """Per-state medical-practice regulatory facts (the medical vertical's
    Gap M1 foundation). Encodes the 10 research domains: licensure, scope/
    delegation, records, telemedicine, informed consent, advertising, data
    security, CME, controlled substances, sensitive services. Downstream
    medical-pack features (M2-M10) load these instead of hardcoding."""
    state: str                     # two-letter code, e.g. "MA"
    state_name: str
    board_name: str                # state medical board
    board_url: str
    board_parent_agency: str       # e.g. "NYSED Office of the Professions"
    governing_statute: str         # e.g. "MGL c. 112"
    statute_url: str               # primary source

    # Licensure + renewal
    license_cycle_years: int       # 1=annual (CT), 2=biennial (most)
    license_renewal_note: str      # e.g. "on birthday", "in birth month"
    imlc_member: bool              # Interstate Medical Licensure Compact (PA + MA are False)
    imlc_citation: str

    # Scope / delegation / corporate practice
    np_supervision_model: SupervisionModel
    np_supervision_citation: str
    corporate_practice_prohibited: bool   # non-physician ownership of medical practices
    corporate_practice_citation: str
    upl_statute: str                # unauthorized practice of medicine citation

    # Medical records
    record_retention_adult_years: int    # 5 (FL) to 7 (MA, TN)
    record_retention_minor_years: int    # until 21+, or N years after last visit
    record_retention_minor_rule: str     # e.g. "until age 21 or 7yr after last visit, whichever longer"
    record_retention_citation: str
    patient_access_days: int            # business days to produce records on request (NY=10)
    patient_access_citation: str

    # Telemedicine
    telemedicine_requirement: TelemedicineRequirement
    telemedicine_prior_in_person: bool  # True if a prior in-person exam is required
    telemedicine_citation: str
    cross_state_practice_allowed: bool  # may an out-of-state physician treat in-state patients (rarely True)
    cross_state_citation: str

    # Informed consent
    written_consent_procedures: str      # what requires written consent (summary)
    telemedicine_consent_documented: bool # consent for telehealth must be documented

    # Advertising
    advertising_filing_required: bool
    advertising_restrictions: str        # testimonials, "best doctor" claims, etc.

    # Data security (same 3 tiers as the law-firm vertical)
    data_security_tier: DataSecurityTier
    data_security_citation: str
    breach_notification_days: int        # 30 (MA) to 60 (HIPAA floor) — 0 if "without unreasonable delay"
    breach_notification_citation: str

    # CME
    cme_required: bool
    cme_hours: int                       # per cycle (PA + MA = 100, CT = 50, FL = 40)
    cme_cycle_years: int                 # 1=annual (CT), 2=biennial (most)
    cme_mandatory_topics: tuple[str, ...]   # per-state (FL: CS/DV/HIV/trafficking; CT: 6yr cycles)
    cme_topic_cycle_years: int           # 0=no separate topic cycle; 6 for CT
    cme_citation: str

    # Controlled substances
    pmp_query_required: bool             # prescription monitoring program query before Rx
    pmp_citation: str
    initial_opioid_rx_limit_days: int    # 7 (NY, MA) or 0 if no state limit
    initial_opioid_rx_citation: str
    mat_buprenorphine_permitted: bool    # DEA-registered prescribers (post X-waiver elimination)
    mat_citation: str

    # Sensitive services — minor consent + record access
    minor_consent_services: tuple[str, ...]  # what minors may self-consent to (reproductive, STI, BH, SU)
    minor_parent_access_restricted: bool     # parent portal access restricted for those encounters
    minor_consent_citation: str

    confidence: str = "primary_source"   # primary_source | established_knowledge | mixed


# ---------------------------------------------------------------------------
# Loader — imports the state module on demand. Returns None if absent.

_STATES = (
    "fl", "ga", "sc", "tn", "va", "wv", "md", "pa", "oh", "nj",
    "ny", "ct", "ma",
)


def _load(state: str, attr: str) -> object | None:
    """Import ``jurisdictions.<state>`` and return its ``attr`` attribute."""
    if state.lower() not in _STATES:
        return None
    try:
        mod = __import__(f"hybridagent.jurisdictions.{state.lower()}",
                         fromlist=[attr])
    except ImportError:
        return None
    return getattr(mod, attr, None)


def get_forensic_profile(state: str) -> ForensicProfile | None:
    """Return the forensic-engineering regulatory profile for ``state``
    (two-letter code), or ``None`` if the state isn't in the registry."""
    prof = _load(state, "FORENSIC")
    return prof if isinstance(prof, ForensicProfile) else None


def get_legal_profile(state: str) -> LegalProfile | None:
    """Return the law-firm regulatory profile for ``state``, or ``None``."""
    prof = _load(state, "LEGAL")
    return prof if isinstance(prof, LegalProfile) else None


def get_medical_profile(state: str) -> MedicalProfile | None:
    """Return the medical-practice regulatory profile for ``state`` (two-letter
    code), or ``None`` if the state isn't in the registry. Downstream medical-
    pack features (M2-M10) call this instead of hardcoding state rules."""
    prof = _load(state, "MEDICAL")
    return prof if isinstance(prof, MedicalProfile) else None


def registered_states() -> tuple[str, ...]:
    """Return the two-letter codes of all states with registry entries."""
    return _STATES


def forensic_summary() -> list[dict]:
    """Compact summary of all 13 forensic profiles — for dashboards/CLI."""
    out = []
    for st in _STATES:
        p = get_forensic_profile(st)
        if p:
            out.append({
                "state": p.state, "admissibility": p.admissibility_standard,
                "firm_coa": p.firm_coa_required,
                "pdh": f"{p.pdh_hours}/{p.pdh_cycle_years}y",
                "confidence": p.confidence,
            })
    return out


def legal_summary() -> list[dict]:
    """Compact summary of all 13 legal profiles."""
    out = []
    for st in _STATES:
        p = get_legal_profile(st)
        if p:
            out.append({
                "state": p.state, "cle": p.cle_required,
                "ad_filing": p.advertising_filing_required,
                "data_security": p.data_security_tier,
                "confidence": p.confidence,
            })
    return out