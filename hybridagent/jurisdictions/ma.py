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
    ForensicProfile,
    LegalProfile,
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