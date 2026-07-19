"""Vertical pack registry.

The base Praxis distribution ships with an **empty** vertical registry.
Vertical packs (law firm, medical office, education, homeschool, forensic
engineering, ...) live in separate, optional distributions and register
themselves at install/import time.

This module is the single, dependency-free extension point between the
open-core base (``smf-praxis``) and the private paid vertical builds::

    from hybridagent.verticals.registry import (
        register_vertical_spec,
        register_vertical_routes,
        register_vertical_eval_cases,
        iter_vertical_specs,
        iter_vertical_routes,
        iter_vertical_eval_cases,
    )

Base alone yields zero vertical specs, zero vertical routes, and zero
vertical eval cases. Installing a private vertical distribution populates
the registry, lighting up its pack, dashboard routes, and eval cases.

Design rules (non-negotiable):

- **Zero base deps.** This module imports nothing from the rest of
  ``hybridagent`` at module load except the lightweight ``VerticalSpec``
  and ``RiskClass`` types used to *describe* registered verticals.
- **Import-free registration.** Callers register by passing already-imported
  objects, never by name lookup. This keeps base unreachable from a
  vertical's import path until that vertical is actually installed.
- **Empty by default.** The base distribution MUST ship this file with all
  registry lists empty. Vertical repos append to the lists on import.
- **Process-global.** The registry is module-level state, intentionally
  process-global so a single ``import hybridagent_verticals_legal`` lights
  up the legal vertical for the whole process lifetime.

The vertical-content modules themselves (e.g. ``hipaa_governance``,
``legal_hold``, ``homeschool_compliance``) continue to be imported
**lazily** by the daemon routes and vertical eval cases that need them.
This registry only describes which verticals are *available*; resolving a
vertical's content modules remains on-demand.

See ``docs/PACKS.md`` for the vertical-pack authoring guide.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol, runtime_checkable

# Lightweight re-export of RiskClass for vertical spec authors. Importing
# here keeps the vertical-spec author path single-source.
from ..broker import RiskClass  # noqa: F401  (re-exported for vertical authors)

# ---------------------------------------------------------------------------
# VerticalSpec — declarative metadata describing a vertical pack.
# ---------------------------------------------------------------------------


@dataclass
class VerticalSpec:
    """Declarative description of a registered vertical pack.

    Attributes:
        name: Pack name (matches a ``hybridagent/packs/<name>/pack.json``
            ``name`` field). Example: ``"law_firm"``.
        persona_keyword: Free-text keyword expected in the pack's
            ``system_prompt``. Used by the generic ``vertical.*.persona``
            eval case to assert the pack ships a real domain persona.
        compliance_mode: One of ``"enforced"``, ``"autonomous"``,
            ``"permissive"``. Drives the generic
            ``vertical.*.posture`` eval case.
        autonomous: Set of ``RiskClass`` the vertical's broker allows
            without human approval (typically ``{READ, DRAFT}``).
        held: Set of ``RiskClass`` the vertical's broker holds for
            approval (typically ``{SEND, DESTRUCTIVE}``).
        version: Optional vertical-pack version string, for diagnostics.
    """

    name: str
    persona_keyword: str
    compliance_mode: str
    autonomous: set = field(default_factory=set)
    held: set = field(default_factory=set)
    version: str = ""


# ---------------------------------------------------------------------------
# Protocols for the two pluggable surfaces (routes, eval cases).
# ---------------------------------------------------------------------------


@runtime_checkable
class RouteRegistrar(Protocol):
    """Callable that mounts a vertical's dashboard routes onto a daemon.

    Vertical distributions provide a registrar implementing this protocol
    and register it via :func:`register_vertical_routes`. The daemon
    invokes every registered registrar at dashboard-construction time.
    """

    def __call__(self, daemon: object) -> None: ...  # pragma: no cover


@runtime_checkable
class EvalCaseFactory(Protocol):
    """Callable returning a list of vertical-specific ``EvalCase`` objects.

    Vertical distributions provide a factory and register it via
    :func:`register_vertical_eval_cases`. ``vertical_evals`` invokes every
    registered factory at eval-collection time.
    """

    def __call__(self) -> list: ...  # pragma: no cover


# ---------------------------------------------------------------------------
# Registry state. Empty by default in base.
# ---------------------------------------------------------------------------

_VERTICAL_SPECS: list[VerticalSpec] = []
_VERTICAL_ROUTE_REGISTRARS: list[RouteRegistrar] = []
_VERTICAL_EVAL_FACTORIES: list[EvalCaseFactory] = []
_REGISTERED_PACK_NAMES: set[str] = set()


# ---------------------------------------------------------------------------
# Registration API (called by vertical distributions on import).
# ---------------------------------------------------------------------------


def register_vertical_spec(spec: VerticalSpec) -> None:
    """Append a :class:`VerticalSpec` describing an installed vertical pack.

    Idempotent: re-registering a spec with the same ``name`` replaces the
    prior entry (last registration wins). This is intentional so that
    vertical distributions can be re-imported (e.g. in tests) without
    duplicating rows.
    """

    if not isinstance(spec, VerticalSpec):
        raise TypeError(
            f"register_vertical_spec expects VerticalSpec, got {type(spec).__name__}"
        )
    for i, existing in enumerate(_VERTICAL_SPECS[:]):
        if existing.name == spec.name:
            _VERTICAL_SPECS[i] = spec
            _REGISTERED_PACK_NAMES.add(spec.name)
            return
    _VERTICAL_SPECS.append(spec)
    _REGISTERED_PACK_NAMES.add(spec.name)


def register_vertical_routes(registrar: RouteRegistrar) -> None:
    """Append a callable that mounts a vertical's dashboard routes."""

    if not callable(registrar):
        raise TypeError(
            f"register_vertical_routes expects a callable, got {type(registrar).__name__}"
        )
    if registrar in _VERTICAL_ROUTE_REGISTRARS:
        return
    _VERTICAL_ROUTE_REGISTRARS.append(registrar)


def register_vertical_eval_cases(factory: EvalCaseFactory) -> None:
    """Append a callable returning a list of vertical ``EvalCase`` objects."""

    if not callable(factory):
        raise TypeError(
            f"register_vertical_eval_cases expects a callable, got {type(factory).__name__}"
        )
    if factory in _VERTICAL_EVAL_FACTORIES:
        return
    _VERTICAL_EVAL_FACTORIES.append(factory)


# ---------------------------------------------------------------------------
# Read API (called by base: daemon, vertical_evals, CLI).
# ---------------------------------------------------------------------------


def iter_vertical_specs() -> Iterable[VerticalSpec]:
    """Iterate every registered :class:`VerticalSpec`."""

    return iter(_VERTICAL_SPECS)


def iter_vertical_routes() -> Iterable[RouteRegistrar]:
    """Iterate every registered dashboard-route registrar."""

    return iter(_VERTICAL_ROUTE_REGISTRARS)


def iter_vertical_eval_factories() -> Iterable[EvalCaseFactory]:
    """Iterate every registered vertical eval-case factory."""

    return iter(_VERTICAL_EVAL_FACTORIES)


def get_vertical_spec(name: str) -> Optional[VerticalSpec]:
    """Return the registered :class:`VerticalSpec` for ``name`` or ``None``."""

    for spec in _VERTICAL_SPECS:
        if spec.name == name:
            return spec
    return None


def is_vertical_registered(name: str) -> bool:
    """True iff a vertical pack named ``name`` has been registered."""

    return name in _REGISTERED_PACK_NAMES


def registered_vertical_names() -> list[str]:
    """Return the names of every registered vertical, in registration order."""

    return [spec.name for spec in _VERTICAL_SPECS]


def clear_registry() -> None:
    """Clear every vertical registration. Intended for tests only."""

    _VERTICAL_SPECS.clear()
    _VERTICAL_ROUTE_REGISTRARS.clear()
    _VERTICAL_EVAL_FACTORIES.clear()
    _REGISTERED_PACK_NAMES.clear()


__all__ = [
    "VerticalSpec",
    "RouteRegistrar",
    "EvalCaseFactory",
    "register_vertical_spec",
    "register_vertical_routes",
    "register_vertical_eval_cases",
    "iter_vertical_specs",
    "iter_vertical_routes",
    "iter_vertical_eval_factories",
    "get_vertical_spec",
    "is_vertical_registered",
    "registered_vertical_names",
    "clear_registry",
]
