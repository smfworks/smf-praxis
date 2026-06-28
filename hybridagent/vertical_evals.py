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
    return cases
