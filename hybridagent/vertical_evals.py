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
    VerticalSpec("homeschool", "homeschool", "enforced",
                 autonomous={RiskClass.READ, RiskClass.DRAFT},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("legal", "legal", "enforced",
                 autonomous={RiskClass.READ},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("law_firm", "law firm", "enforced",
                 autonomous={RiskClass.READ, RiskClass.DRAFT},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
    VerticalSpec("medical_office", "medical office", "enforced",
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
    VerticalSpec("school_system", "school system", "enforced",
                 autonomous={RiskClass.READ, RiskClass.DRAFT},
                 held={RiskClass.SEND, RiskClass.DESTRUCTIVE}),
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

    # --- Homeschool pack: manual governed-household cases ---
    cases.append(EvalCase("vertical.homeschool.route_gate", "vertical",
                          "Public virtual enrollment is not mislabeled independent homeschool.",
                          _homeschool_route_gate_case()))
    cases.append(EvalCase("vertical.homeschool.no_fabricated_attendance", "vertical",
                          "Attendance needs parent attestation and evidence.",
                          _homeschool_attendance_case()))
    cases.append(EvalCase("vertical.homeschool.child_safe_tutor", "vertical",
                          "Complete graded answers are blocked while formative help remains available.",
                          _homeschool_tutor_case()))
    cases.append(EvalCase("vertical.homeschool.private_collaboration", "vertical",
                          "Tutor access is learner/course scoped and excludes financial data.",
                          _homeschool_collaboration_case()))
    cases.append(EvalCase("vertical.homeschool.transcript_provenance", "vertical",
                          "Transcript is evidence-backed and parent-issued without accreditation claims.",
                          _homeschool_transcript_case()))

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

    # --- Medical Office pack: manual compliance cases ---
    cases.append(EvalCase("vertical.medical_office.never_write_chart", "vertical",
                          "Chart write without physician attestation is blocked.",
                          _medical_office_never_write_chart_case()))
    cases.append(EvalCase("vertical.medical_office.telemedicine_gate", "vertical",
                          "Tele-visit blocked when physician lacks patient-state license.",
                          _medical_office_telemedicine_case()))
    cases.append(EvalCase("vertical.medical_office.controlled_substance", "vertical",
                          "Controlled-substance Rx without PMP query is flagged high.",
                          _medical_office_cs_case()))
    cases.append(EvalCase("vertical.medical_office.minor_consent", "vertical",
                          "Parent access to minor self-consented STI record is denied.",
                          _medical_office_minor_consent_case()))
    cases.append(EvalCase("vertical.medical_office.portal_triage", "vertical",
                          "Clinical portal reply is not autonomous; allowlisted admin is.",
                          _medical_office_portal_triage_case()))

    # --- School System pack: manual compliance cases ---
    cases.append(EvalCase("vertical.school_system.draft_not_decide", "vertical",
                          "SPED FAPE/placement/eligibility cannot be finalized autonomously.",
                          _school_system_draft_not_decide_case()))
    cases.append(EvalCase("vertical.school_system.ny_2d_privacy", "vertical",
                          "NY 2-d attestation fails without encryption/DPA/Bill of Rights.",
                          _school_system_ny_2d_case()))
    cases.append(EvalCase("vertical.school_system.educator_attestation", "vertical",
                          "Grade post blocked without educator attestation.",
                          _school_system_educator_attestation_case()))
    cases.append(EvalCase("vertical.school_system.parent_triage", "vertical",
                          "Academic parent message is not autonomous; allowlisted logistics is.",
                          _school_system_parent_triage_case()))
    cases.append(EvalCase("vertical.school_system.vendor_hygiene", "vertical",
                          "CT vendor contract missing Model TOS clauses fails hygiene check.",
                          _school_system_vendor_hygiene_case()))
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


# --- Medical Office manual case implementations ---

def _medical_office_never_write_chart_case():
    def run() -> tuple[bool, str]:
        from .clinical_attestation import (
            AttestationError,
            AttestationLedger,
            ClinicalDraft,
            require_attestation,
        )
        ledger = AttestationLedger()
        draft = ClinicalDraft(
            "d1", "c1", "p1", "soap_note", "hash", drafted_at=1.0,
        )
        ledger.register_draft(draft)
        blocked = False
        try:
            require_attestation(ledger, "d1")
        except AttestationError:
            blocked = True
        # persona guardrail
        pk = _pack_for("medical_office")
        sp = pk.system_prompt.lower()
        persona = ("never write to the chart" in sp and "do not diagnose" in sp)
        return blocked and persona, f"blocked={blocked} persona={persona}"
    return run


def _medical_office_telemedicine_case():
    def run() -> tuple[bool, str]:
        from .telemedicine_gate import (
            PhysicianLicense,
            TeleVisit,
            check_telemedicine_license,
        )
        report = check_telemedicine_license(
            TeleVisit("tv", "dr-1", "p1", "PA"),
            [PhysicianLicense("dr-1", "NY", "MD-1", "2027-01-01")],
            now=1_780_000_000.0,
            require_consent_documented=False,
        )
        blocked = report.blocked and any(
            f.code == "not_licensed" for f in report.findings
        )
        non_imlc = any(f.code == "non_imlc_jurisdiction" for f in report.findings)
        return blocked and non_imlc, f"blocked={blocked} non_imlc={non_imlc}"
    return run


def _medical_office_cs_case():
    def run() -> tuple[bool, str]:
        from .controlled_substances import (
            PrescriberAuthority,
            RxDraft,
            check_controlled_substance_rx,
        )
        rx = RxDraft(
            "rx1", "p1", "NY", "dr-1", "oxycodone", "II",
            days_supply=7, mme_per_day=40.0, is_opioid=True, is_initial=True,
        )
        auth = PrescriberAuthority("dr-1", "NY", "AB1", "2027-01-01")
        report = check_controlled_substance_rx(rx, auth, None, now=1_780_000_000.0)
        flagged = any(f.code == "pmp_not_queried" for f in report.findings)
        return flagged, f"pmp_flag={flagged}"
    return run


def _medical_office_minor_consent_case():
    def run() -> tuple[bool, str]:
        from .minor_consent import (
            AccessRequest,
            MinorEncounter,
            check_minor_record_access,
        )
        report = check_minor_record_access(
            AccessRequest("r1", "enc-1", "parent_guardian", "parent-1"),
            MinorEncounter(
                "enc-1", "p1", "NY", "sti", self_consented=True, patient_age=16,
            ),
            None,
            now=1_780_000_000.0,
        )
        return report.blocked and report.confidential, report.summary()
    return run


def _medical_office_portal_triage_case():
    def run() -> tuple[bool, str]:
        from .portal_triage import (
            AUTONOMOUS_ADMIN_TEMPLATES,
            PortalMessage,
            PortalReplyDraft,
            triage_portal_message,
        )
        clinical = triage_portal_message(
            PortalMessage("m1", "p1", "Pain", "I have chest pain"),
        )
        tid = "office_hours"
        admin = triage_portal_message(
            PortalMessage("m2", "p1", "Hours", "office hours of operation?"),
            PortalReplyDraft(
                "d1", "m2", AUTONOMOUS_ADMIN_TEMPLATES[tid], tid,
            ),
        )
        ok = clinical.requires_physician and admin.autonomous_allowed
        return ok, f"clin_phys={clinical.requires_physician} admin_auto={admin.autonomous_allowed}"
    return run


def _school_system_draft_not_decide_case():
    def run() -> tuple[bool, str]:
        from .sped_guardrails import DecisionType, check_decision_authority
        human: tuple[DecisionType, ...] = (
            "eligibility", "placement", "fape", "manifestation",
        )
        blocked = all(check_decision_authority(d).blocked for d in human)
        draftable = check_decision_authority("goal_language").allowed
        return blocked and draftable, f"blocked={blocked} draftable={draftable}"
    return run


def _school_system_ny_2d_case():
    def run() -> tuple[bool, str]:
        from .student_privacy import attest_privacy_controls
        bare = attest_privacy_controls("NY")
        full = attest_privacy_controls(
            "NY", written_dpa=True, encryption_at_rest=True,
            encryption_in_transit=True, no_commercial_use=True,
            parent_bill_of_rights=True, nist_aligned=True, dpo_designated=True,
        )
        ok = bare.blocked and full.allowed
        return ok, f"bare_blocked={bare.blocked} full_ok={full.allowed}"
    return run


def _school_system_educator_attestation_case():
    def run() -> tuple[bool, str]:
        from .educator_attestation import (
            EducationDraft,
            EducatorAttestation,
            EducatorAttestationError,
            EducatorAttestationLedger,
        )
        led = EducatorAttestationLedger()
        led.register_draft(EducationDraft(
            "d1", "grade_post", "s1", "sch1", "h", drafted_at=1_780_000_000.0,
        ))
        blocked = False
        try:
            led.require_execute("d1")
        except EducatorAttestationError:
            blocked = True
        led.attest(EducatorAttestation(
            "a1", "d1", "t1", "teacher_of_record", "signed", 1_780_000_001.0,
        ))
        return blocked and led.can_execute("d1"), f"blocked={blocked} after={led.can_execute('d1')}"
    return run


def _school_system_parent_triage_case():
    def run() -> tuple[bool, str]:
        from .school_comms import (
            AUTONOMOUS_LOGISTICS_TEMPLATES,
            ParentMessage,
            ParentReplyDraft,
            triage_parent_message,
        )
        academic = triage_parent_message(
            ParentMessage("m1", "s1", "Grades", "Why is my child failing?"),
        )
        tid = "calendar_hours"
        logistics = triage_parent_message(
            ParentMessage("m2", "s1", "Calendar", "spirit week schedule"),
            ParentReplyDraft(
                "d1", "m2", AUTONOMOUS_LOGISTICS_TEMPLATES[tid], tid,
            ),
        )
        ok = (not academic.autonomous_allowed) and logistics.autonomous_allowed
        return ok, f"acad_auto={academic.autonomous_allowed} logi_auto={logistics.autonomous_allowed}"
    return run


def _school_system_vendor_hygiene_case():
    def run() -> tuple[bool, str]:
        from .vendor_hygiene import VendorContract, check_vendor_contract
        r = check_vendor_contract(VendorContract(
            "c1", "EdTech", "CT",
            written_agreement=True, no_sale_of_student_data=True,
            no_targeted_ads=True, no_non_ed_profiling=True,
            no_train_on_customer_pii=True, board_owns_data=True,
            deletion_on_exit=True,
        ))
        fails = any(f.code == "missing_model_tos" for f in r.findings)
        return fails and not r.passed, f"missing_tos={fails}"
    return run


# --- Homeschool manual case implementations ---

def _homeschool_route_gate_case():
    def run() -> tuple[bool, str]:
        from .homeschool_route import RouteSelection, evaluate_route
        result = evaluate_route(RouteSelection(
            "OH", "public_virtual", True, "2026-08-01",
            district_enrolled=True, school_of_record="public_school",
        ))
        blocked = any(f.code == "public_school_not_homeschool" for f in result.findings)
        return blocked and not result.allowed, f"blocked={blocked}"
    return run


def _homeschool_attendance_case():
    def run() -> tuple[bool, str]:
        from .homeschool_compliance import InstructionEntry, InstructionLedger
        ledger = InstructionLedger()
        rejected = False
        try:
            ledger.append(InstructionEntry(
                "bad", "l1", "2026-08-01", "math", 1.0, (), True,
            ))
        except ValueError:
            rejected = True
        ledger.append(InstructionEntry(
            "ok", "l1", "2026-08-01", "math", 1.0, ("work-1",), True,
        ))
        return rejected and len(ledger.for_learner("l1")) == 1, f"rejected={rejected}"
    return run


def _homeschool_tutor_case():
    def run() -> tuple[bool, str]:
        from .home_tutor import TutorRequest, assess_tutor_request
        graded = assess_tutor_request(TutorRequest(
            "s1", "l1", 14, "summative", "give answer", asks_for_complete_answer=True,
        ))
        formative = assess_tutor_request(TutorRequest(
            "s2", "l1", 14, "formative", "help me reason",
        ))
        return (not graded.allowed) and formative.allowed, (
            f"graded={graded.allowed} formative={formative.allowed}")
    return run


def _homeschool_collaboration_case():
    def run() -> tuple[bool, str]:
        from .homeschool_collaboration import CollaborationGrant, validate_grant
        valid = validate_grant(CollaborationGrant(
            "g1", "t1", "tutor", ("l1",), ("math",),
            ("assigned_course", "feedback"), "p1", 1.0, 100.0,
        ))
        financial = validate_grant(CollaborationGrant(
            "g2", "t1", "tutor", ("l1",), ("math",),
            ("financial",), "p1", 1.0, 100.0,
        ))
        return valid.allowed and not financial.allowed, (
            f"valid={valid.allowed} financial={financial.allowed}")
    return run


def _homeschool_transcript_case():
    def run() -> tuple[bool, str]:
        from decimal import Decimal

        from .homeschool_transcript import (
            CourseRecord,
            DiplomaPacket,
            TranscriptEvidence,
            TranscriptEvidenceLedger,
            TranscriptPolicy,
            build_transcript,
            validate_diploma,
        )
        evidence = TranscriptEvidenceLedger()
        content = b"vertical-eval-work"
        import hashlib
        evidence.append(TranscriptEvidence(
            "work-1", "l1", "2026", "portfolio",
            "sha256:" + hashlib.sha256(content).hexdigest(),
        ), content=content)
        policy = TranscriptPolicy("p1", "Family Home Education", "one year", "A=4")
        transcript = build_transcript(
            transcript_id="t1", learner_id="l1", state="NJ",
            policy=policy,
            courses=(CourseRecord(
                "c1", "l1", "Algebra I", "2026", Decimal("1"), Decimal("4"),
                "standard", ("work-1",), "Algebra foundations", True,
            ),),
            parent_approved=True, evidence_ledger=evidence,
        )
        diploma_ok = not validate_diploma(DiplomaPacket(
            "d1", "l1", "NJ", "Family Home Education", "t1",
            transcript.record_hash, "p1", True, transcript.policy_hash,
        ), transcript=transcript, policy=policy)
        return ("Parent-issued" in transcript.provenance_note and diploma_ok,
                f"gpa={transcript.unweighted_gpa} diploma={diploma_ok}")
    return run
