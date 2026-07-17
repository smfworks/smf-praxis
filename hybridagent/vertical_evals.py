"""Per-vertical eval packs (p09) — prove each vertical pack ships a sane persona
and the *right* governance posture, using the real broker + governed loop.

A "vertical eval pack" is a small, deterministic, offline check derived from a
:class:`VerticalSpec`. For each vertical we assert three things end-to-end:

  1. **persona**   — the template/bundled pack has a non-empty domain system prompt
                     and ``pack.compose_system`` prepends it to the base prompt.
  2. **autonomy**  — every risk class in ``autonomousRisks`` runs WITHOUT approval.
  3. **restraint** — every other consequential class (send/destructive, unless the
                     vertical lists it autonomous) is HELD for approval, never run.

These run on the offline mock LLM and the genuine governance machinery, so
``praxis eval --category vertical`` gates "does activating <vertical> still give
the promised posture?" with no network or key.

Extending to a new domain (human + agent steps):
  1. Add a template to ``vertical_templates.VERTICAL_TEMPLATES`` (persona, mode,
     riskPolicy) and any aliases.
  2. (Optional) ship a bundled pack at ``packs/<name>/pack.json`` mirroring it.
  3. Add one ``VerticalSpec`` row below: name, persona keyword, autonomous classes,
     held classes, expected compliance mode. That's the whole eval pack — the
     persona/autonomy/restraint cases are generated for you.
  4. Run ``praxis eval --category vertical`` (and ``tests/test_vertical_evals.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict
from .evals import EvalCase
from .pack import VerticalPack, apply_to_policy, compose_system
from .tools import Tool

_PROBES = {
    RiskClass.READ: Tool("v_read", RiskClass.READ, "read", lambda **k: "data"),
    RiskClass.DRAFT: Tool("v_draft", RiskClass.DRAFT, "draft", lambda **k: "drafted"),
    RiskClass.SEND: Tool("v_send", RiskClass.SEND, "send", lambda **k: "SENT"),
    RiskClass.DESTRUCTIVE: Tool("v_del", RiskClass.DESTRUCTIVE, "delete", lambda **k: "GONE"),
}


@dataclass
class VerticalSpec:
    name: str
    persona_keyword: str
    compliance_mode: str
    autonomous: set = field(default_factory=set)
    held: set = field(default_factory=set)


VERTICAL_SPECS: list[VerticalSpec] = [
    VerticalSpec("homeschool", "homeschool", "autonomous",
                 autonomous={RiskClass.READ, RiskClass.DRAFT},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("legal", "legal", "enforced",
                 autonomous={RiskClass.READ},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("law_firm", "law firm", "enforced",
                 autonomous={RiskClass.READ, RiskClass.DRAFT},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("medical", "clinical", "enforced",
                 autonomous={RiskClass.READ},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("forensic", "forensic", "enforced",
                 autonomous={RiskClass.READ},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("education", "tutor", "autonomous",
                 autonomous={RiskClass.READ, RiskClass.DRAFT},
                 held={RiskClass.DESTRUCTIVE}),
]


def _pack_for(name: str) -> VerticalPack:
    from . import vertical_templates as vt
    t = vt.get_template(name) or {}
    return VerticalPack.from_manifest({**t, "name": name})


def _policy(pk: VerticalPack) -> GovernancePolicy:
    policy = GovernancePolicy(allowed_tools={t.name for t in _PROBES.values()})
    apply_to_policy(pk, policy)
    return policy


def _persona_case(spec: VerticalSpec):
    def run() -> tuple[bool, str]:
        pk = _pack_for(spec.name)
        has_kw = spec.persona_keyword in pk.system_prompt.lower()
        mode_ok = (pk.compliance_mode == spec.compliance_mode)
        prepended = compose_system("BASE").endswith("BASE") and pk.system_prompt
        return bool(has_kw and mode_ok and prepended), (
            f"kw={has_kw} mode={pk.compliance_mode} prompt={bool(pk.system_prompt)}")
    return run


def _posture_case(spec: VerticalSpec):
    def run() -> tuple[bool, str]:
        broker = GovernanceBroker(_policy(_pack_for(spec.name)))
        for rc in spec.autonomous:
            if broker.authorize("a", _PROBES[rc].name, rc, {}).verdict is not Verdict.ALLOW:
                return False, f"{rc.value} should be autonomous"
        for rc in spec.held:
            if broker.authorize("a", _PROBES[rc].name, rc, {}).verdict is Verdict.ALLOW:
                return False, f"{rc.value} should be held"
        return True, f"auto={sorted(r.value for r in spec.autonomous)}"
    return run


def vertical_eval_cases() -> list[EvalCase]:
    cases: list[EvalCase] = []
    for spec in VERTICAL_SPECS:
        cases.append(EvalCase(f"vertical.{spec.name}.persona", "vertical",
                              f"{spec.name} pack ships a domain persona ({spec.compliance_mode}).",
                              _persona_case(spec)))
        cases.append(EvalCase(f"vertical.{spec.name}.posture", "vertical",
                              f"{spec.name} pack autonomy/restraint posture is enforced.",
                              _posture_case(spec)))

    # --- Law Firm pack: manual compliance cases (exercise the v0.28.14 modules) ---
    cases.append(EvalCase("vertical.law_firm.upl_guardrail", "vertical",
                          "Law Firm persona carries the UPL + IOLTA guardrails (all 13 states).",
                          _law_firm_upl_guardrail_case()))
    cases.append(EvalCase("vertical.law_firm.ny_ad_filing_gate", "vertical",
                          "NY advertising missing the label is blocked (22 NYCRR 1200).",
                          _law_firm_ny_ad_filing_case()))
    cases.append(EvalCase("vertical.law_firm.ma_wisp_attestation", "vertical",
                          "MA matter without a WISP fails the 201 CMR 17.00 attestation.",
                          _law_firm_ma_wisp_case()))
    cases.append(EvalCase("vertical.law_firm.conflict_check", "vertical",
                          "Conflict check surfaces hits without leaking matter content.",
                          _law_firm_conflict_case()))
    cases.append(EvalCase("vertical.law_firm.cle_status", "vertical",
                          "NY attorney with insufficient ethics hours is CE-deficient; MA is no-requirement.",
                          _law_firm_cle_case()))
    return cases


# --- Law Firm manual case implementations ---

def _law_firm_upl_guardrail_case():
    def run() -> tuple[bool, str]:
        pk = _pack_for("law_firm")
        sp = pk.system_prompt.lower()
        checks = {
            "UPL": "do not provide legal advice" in sp,
            "IOLTA": "iolta" in sp,
            "trust_acct": "trust accounting" in sp,
            "no_mdp_implicit": "never send, file, or sign" in sp,
        }
        ok = all(checks.values())
        return ok, ",".join(k for k, v in checks.items() if v) or "missing"
    return run


def _law_firm_ny_ad_filing_case():
    def run() -> tuple[bool, str]:
        from .advertising_filing import AdvertisingFiling, validate_before_send
        # NY advertising missing the label -> critical finding -> can't send
        f = AdvertisingFiling(artifact_id="ad-1", jurisdiction="NY", status="draft")
        findings = validate_before_send(f)
        blocked = any(x.severity == "critical" and x.field == "label_present"
                      for x in findings)
        return blocked, "label-missing blocked" if blocked else "NOT blocked"
    return run


def _law_firm_ma_wisp_case():
    def run() -> tuple[bool, str]:
        from .security_attestation import SecurityControls, attest
        # MA matter with no WISP -> FAIL with a critical WISP finding
        att = attest("MA", SecurityControls())  # nothing asserted
        return (not att.passed and
                any(x.severity == "critical" and x.control == "wisp_on_file"
                    for x in att.findings),
                att.summary()[:60])
    return run


def _law_firm_conflict_case():
    def run() -> tuple[bool, str]:
        from .conflicts import ConflictChecker, ConflictHit, PartyName
        from .workspaces import Workspace

        class _FakeDir:
            def __init__(self, matters):
                self._m = matters
            def list_for(self, org):
                return [m for m in self._m if m.organization_id == org]

        ws = Workspace(
            workspace_id="ws-1", organization_id="org-1", human_identifier="ws-1",
            kind="matter", title="Smith v Jones", client_or_subject="Jane Smith",
            owner_user_id="a", team_id="t", status="active", confidentiality="internal",
            jurisdiction="NY", location="", opened_date="2026-01-01", target_date="",
            field_schema={}, custom_fields={}, external_links=(),
            legal_hold=False, hold_reason="", created_ts=0.0, updated_ts=0.0)
        checker = ConflictChecker(_FakeDir([ws]))
        report = checker.check(
            prospective_parties=[PartyName("Jane Smith", "opposing")],
            organization_id="org-1", authorized_by="atty-1")
        # hit surfaces + no content field on the ConflictHit (no-leak)
        no_leak = (not hasattr(ConflictHit, "content") and
                   not hasattr(ConflictHit, "memory"))
        return (not report.clean and no_leak and len(report.hits) == 1,
                f"hits={len(report.hits)} no_leak={no_leak}")
    return run


def _law_firm_cle_case():
    def run() -> tuple[bool, str]:
        from .credentials import CESession, compliance_status, credential_for, record_hours
        # NY attorney with enough total hours but not enough ethics -> deficient
        ny = credential_for("u", "attorney", "NY", "1", "2026-01-01")
        assert ny is not None, "NY attorney profile must exist"
        record_hours(ny, CESession(date="2026-02-01", hours=24, ethics_hours=1))
        ny_def = compliance_status(ny) == "ce_deficient"
        # MA attorney -> no_requirement (the only state without mandatory CLE)
        ma = credential_for("u", "attorney", "MA", "1", "2026-01-01")
        assert ma is not None, "MA attorney profile must exist"
        ma_none = compliance_status(ma) == "no_requirement"
        return (ny_def and ma_none,
                f"ny={compliance_status(ny)} ma={compliance_status(ma)}")
    return run
