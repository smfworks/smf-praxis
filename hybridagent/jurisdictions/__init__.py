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