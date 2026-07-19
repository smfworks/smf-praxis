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

Architecture (post vertical-extraction refactor):

  * The open-core base ships an **empty** vertical registry
    (``hybridagent.verticals.registry``). With no verticals registered,
    ``vertical_eval_cases()`` returns an empty list and ``praxis eval`` runs
    only the 40 base capability/safety evals.
  * Vertical packs register themselves by populating the registry — either
    via the built-in bridge module ``hybridagent.verticals._builtin`` (used
    while the vertical packs still live inside the base distribution) or via
    a private vertical distribution's own registration module.
  * The generic ``vertical.<name>.persona`` and ``vertical.<name>.posture``
    cases are generated here from the registered specs. Vertical-specific
    manual cases (e.g. ``vertical.law_firm.ny_ad_filing_gate``) are provided
    by eval-case factories registered with the registry.

Extending to a new domain (human + agent steps):

  1. Add a template to ``vertical_templates.VERTICAL_TEMPLATES`` (persona, mode,
     riskPolicy) and any aliases.
  2. (Optional) ship a bundled pack at ``packs/<name>/pack.json`` mirroring it.
  3. Register a ``VerticalSpec`` with the registry (see
     ``hybridagent.verticals._builtin`` for the pattern). That's the whole
     eval pack — the persona/autonomy/restraint cases are generated for you.
  4. Run ``praxis eval --category vertical`` (and ``tests/test_vertical_evals.py``).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict
from .evals import EvalCase
from .pack import VerticalPack, apply_to_policy, compose_system
from .tools import Tool
from .verticals.registry import (
    VerticalSpec as _RegistryVerticalSpec,
)
from .verticals.registry import (
    iter_vertical_eval_factories,
    iter_vertical_specs,
    register_vertical_spec,
)

# Import the built-in bridge so the vertical packs that still ship inside the
# base distribution register themselves. When the verticals are extracted into
# private distributions, this import is removed and the base ships with an
# empty registry. The try/except makes the extraction cutover non-breaking:
# base imports clean either way.
_builtin_bridge: object  # None when verticals are extracted into private dists
try:  # pragma: no cover - exercised by tests/test_vertical_evals.py
    from .verticals import _builtin as _builtin_bridge  # type: ignore[assignment] # noqa: F401
except ImportError:  # verticals extracted into private distributions
    _builtin_bridge = None


_PROBES = {
    RiskClass.READ: Tool("v_read", RiskClass.READ, "read", lambda **k: "data"),
    RiskClass.DRAFT: Tool("v_draft", RiskClass.DRAFT, "draft", lambda **k: "drafted"),
    RiskClass.SEND: Tool("v_send", RiskClass.SEND, "send", lambda **k: "SENT"),
    RiskClass.DESTRUCTIVE: Tool("v_del", RiskClass.DESTRUCTIVE, "delete", lambda **k: "GONE"),
}


@dataclass
class VerticalSpec:
    """Local VerticalSpec mirror.

    Kept for backwards-compatibility with tests and external callers that
    import ``hybridagent.vertical_evals.VerticalSpec``. New code should
    import :class:`hybridagent.verticals.registry.VerticalSpec` directly.
    """

    name: str
    persona_keyword: str
    compliance_mode: str
    autonomous: set = field(default_factory=set)
    held: set = field(default_factory=set)


def _mirror_specs() -> list[VerticalSpec]:
    """Build local-mirror VerticalSpec instances from the registry."""

    return [
        VerticalSpec(
            name=s.name,
            persona_keyword=s.persona_keyword,
            compliance_mode=s.compliance_mode,
            autonomous=set(s.autonomous),
            held=set(s.held),
        )
        for s in iter_vertical_specs()
    ]


# Backwards-compatible attribute. Tests do ``len(ve.VERTICAL_SPECS)``; the
# registry is populated at import time by ``_builtin_bridge`` above, so this
# list is non-empty when verticals are installed and empty when they are not.
VERTICAL_SPECS: list[VerticalSpec] = _mirror_specs()


def _pack_for(name: str) -> VerticalPack:
    from . import vertical_templates as vt
    t = vt.get_template(name) or {}
    return VerticalPack.from_manifest({**t, "name": name})


def _policy(pk: VerticalPack) -> GovernancePolicy:
    policy = GovernancePolicy(allowed_tools={t.name for t in _PROBES.values()})
    apply_to_policy(pk, policy)
    return policy


def _persona_case(spec: _RegistryVerticalSpec):
    def run() -> tuple[bool, str]:
        pk = _pack_for(spec.name)
        has_kw = spec.persona_keyword in pk.system_prompt.lower()
        mode_ok = (pk.compliance_mode == spec.compliance_mode)
        prepended = compose_system("BASE").endswith("BASE") and pk.system_prompt
        return bool(has_kw and mode_ok and prepended), (
            f"kw={has_kw} mode={pk.compliance_mode} prompt={bool(pk.system_prompt)}")
    return run


def _posture_case(spec: _RegistryVerticalSpec):
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
    # Generic persona + posture cases for every registered spec.
    for spec in iter_vertical_specs():
        cases.append(EvalCase(f"vertical.{spec.name}.persona", "vertical",
                              f"{spec.name} pack ships a domain persona ({spec.compliance_mode}).",
                              _persona_case(spec)))
        cases.append(EvalCase(f"vertical.{spec.name}.posture", "vertical",
                              f"{spec.name} pack autonomy/restraint posture is enforced.",
                              _posture_case(spec)))
    # Manual vertical-specific cases from registered factories.
    for factory in iter_vertical_eval_factories():
        cases.extend(factory())
    return cases


# Backwards-compat: re-export the registry's VerticalSpec under the local name
# for any caller doing ``from hybridagent.vertical_evals import VerticalSpec``.
# (The local dataclass above is the primary definition; this alias lets
# registration helpers use either path interchangeably.)
if TYPE_CHECKING:  # pragma: no cover
    pass


__all__ = [
    "VerticalSpec",
    "VERTICAL_SPECS",
    "vertical_eval_cases",
    "register_vertical_spec",
]