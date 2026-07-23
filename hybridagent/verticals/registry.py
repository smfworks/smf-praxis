"""Dependency-free registry for optional Praxis vertical distributions.

The open-core base ships with empty registry state. Private vertical packages
register specs, eval factories, route registrars, and packaged pack roots when
their ``praxis.verticals`` entry point is loaded.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Iterable, Optional, Protocol, runtime_checkable

from ..broker import RiskClass  # noqa: F401 - re-exported for vertical authors

ENTRY_POINT_GROUP = "praxis.verticals"


@dataclass
class VerticalSpec:
    """Declarative description of an installed vertical pack."""

    name: str
    persona_keyword: str
    compliance_mode: str
    autonomous: set = field(default_factory=set)
    held: set = field(default_factory=set)
    version: str = ""


@runtime_checkable
class RouteRegistrar(Protocol):
    def __call__(self, request_handler: object) -> bool: ...  # pragma: no cover


@runtime_checkable
class EvalCaseFactory(Protocol):
    def __call__(self) -> list: ...  # pragma: no cover


_VERTICAL_SPECS: list[VerticalSpec] = []
_VERTICAL_ROUTE_REGISTRARS: list[RouteRegistrar] = []
_VERTICAL_EVAL_FACTORIES: list[EvalCaseFactory] = []
_VERTICAL_PACK_ROOTS: list[Path] = []
_VERTICAL_WEB_ROOTS: list[Path] = []
_REGISTERED_PACK_NAMES: set[str] = set()
_LOADED_ENTRY_POINTS: set[str] = set()
_VERTICAL_LOAD_ERRORS: dict[str, str] = {}
_AUTOLOAD_IN_PROGRESS = False


def register_vertical_spec(spec: VerticalSpec) -> None:
    """Register ``spec``; repeated names are replaced (last registration wins)."""

    if not isinstance(spec, VerticalSpec):
        raise TypeError(
            f"register_vertical_spec expects VerticalSpec, got {type(spec).__name__}"
        )
    for index, existing in enumerate(_VERTICAL_SPECS):
        if existing.name == spec.name:
            _VERTICAL_SPECS[index] = spec
            _REGISTERED_PACK_NAMES.add(spec.name)
            return
    _VERTICAL_SPECS.append(spec)
    _REGISTERED_PACK_NAMES.add(spec.name)


def register_vertical_routes(registrar: RouteRegistrar) -> None:
    """Register a request handler returning ``True`` when it serves a route."""

    if not callable(registrar):
        raise TypeError(
            f"register_vertical_routes expects a callable, got {type(registrar).__name__}"
        )
    if registrar not in _VERTICAL_ROUTE_REGISTRARS:
        _VERTICAL_ROUTE_REGISTRARS.append(registrar)


def register_vertical_eval_cases(factory: EvalCaseFactory) -> None:
    """Register a callable returning vertical-specific eval cases."""

    if not callable(factory):
        raise TypeError(
            f"register_vertical_eval_cases expects a callable, got {type(factory).__name__}"
        )
    if factory not in _VERTICAL_EVAL_FACTORIES:
        _VERTICAL_EVAL_FACTORIES.append(factory)


def register_vertical_pack_root(root: str | Path) -> None:
    """Register a directory whose immediate children are vertical pack folders."""

    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"vertical pack root does not exist or is not a directory: {path}")
    if path not in _VERTICAL_PACK_ROOTS:
        _VERTICAL_PACK_ROOTS.append(path)


def register_vertical_web_root(root: str | Path) -> None:
    """Register a directory containing static Command Deck assets."""

    path = Path(root).expanduser().resolve()
    if not path.is_dir():
        raise ValueError(f"vertical web root does not exist or is not a directory: {path}")
    if path not in _VERTICAL_WEB_ROOTS:
        _VERTICAL_WEB_ROOTS.append(path)


def _installed_entry_points() -> list:
    discovered = metadata.entry_points()
    select = getattr(discovered, "select", None)
    if callable(select):
        return list(select(group=ENTRY_POINT_GROUP))
    if isinstance(discovered, dict):  # Python 3.10 compatibility
        return list(discovered.get(ENTRY_POINT_GROUP, []))
    return []


def load_installed_verticals(*, force: bool = False) -> dict[str, str]:
    """Load every installed ``praxis.verticals`` entry point exactly once.

    Registration failures are retained and returned instead of being silently
    discarded. Eval collection converts them into failing cases, so a broken
    installed vertical cannot make the base-only eval suite appear green.
    """

    global _AUTOLOAD_IN_PROGRESS
    if _AUTOLOAD_IN_PROGRESS:
        return dict(_VERTICAL_LOAD_ERRORS)

    _AUTOLOAD_IN_PROGRESS = True
    try:
        for entry_point in sorted(
            _installed_entry_points(), key=lambda item: (item.name, item.value)
        ):
            distribution = getattr(getattr(entry_point, "dist", None), "name", "")
            key = f"{distribution}:{entry_point.name}:{entry_point.value}"
            if key in _LOADED_ENTRY_POINTS and not force:
                continue
            if force:
                _VERTICAL_LOAD_ERRORS.pop(entry_point.name, None)
            try:
                hook = entry_point.load()
                if not callable(hook):
                    raise TypeError(
                        f"entry point {entry_point.value!r} did not resolve to a callable"
                    )
                hook()
            except Exception as exc:  # one package must not hide the other packages
                _VERTICAL_LOAD_ERRORS[entry_point.name] = (
                    f"{type(exc).__name__}: {exc}"
                )
            finally:
                _LOADED_ENTRY_POINTS.add(key)
    finally:
        _AUTOLOAD_IN_PROGRESS = False
    return dict(_VERTICAL_LOAD_ERRORS)


def iter_vertical_specs() -> Iterable[VerticalSpec]:
    return iter(tuple(_VERTICAL_SPECS))


def iter_vertical_routes() -> Iterable[RouteRegistrar]:
    return iter(tuple(_VERTICAL_ROUTE_REGISTRARS))


def iter_vertical_eval_factories() -> Iterable[EvalCaseFactory]:
    return iter(tuple(_VERTICAL_EVAL_FACTORIES))


def iter_vertical_pack_roots() -> Iterable[Path]:
    return iter(tuple(_VERTICAL_PACK_ROOTS))


def iter_vertical_web_roots() -> Iterable[Path]:
    return iter(tuple(_VERTICAL_WEB_ROOTS))


def get_vertical_spec(name: str) -> Optional[VerticalSpec]:
    for spec in _VERTICAL_SPECS:
        if spec.name == name:
            return spec
    return None


def is_vertical_registered(name: str) -> bool:
    return name in _REGISTERED_PACK_NAMES


def registered_vertical_names() -> list[str]:
    return [spec.name for spec in _VERTICAL_SPECS]


def vertical_load_errors() -> dict[str, str]:
    return dict(_VERTICAL_LOAD_ERRORS)


def clear_registry() -> None:
    """Clear all process-global registration and autoload state (tests only)."""

    global _AUTOLOAD_IN_PROGRESS
    _VERTICAL_SPECS.clear()
    _VERTICAL_ROUTE_REGISTRARS.clear()
    _VERTICAL_EVAL_FACTORIES.clear()
    _VERTICAL_PACK_ROOTS.clear()
    _VERTICAL_WEB_ROOTS.clear()
    _REGISTERED_PACK_NAMES.clear()
    _LOADED_ENTRY_POINTS.clear()
    _VERTICAL_LOAD_ERRORS.clear()
    _AUTOLOAD_IN_PROGRESS = False


__all__ = [
    "ENTRY_POINT_GROUP",
    "VerticalSpec",
    "RouteRegistrar",
    "EvalCaseFactory",
    "register_vertical_spec",
    "register_vertical_routes",
    "register_vertical_eval_cases",
    "register_vertical_pack_root",
    "register_vertical_web_root",
    "load_installed_verticals",
    "iter_vertical_specs",
    "iter_vertical_routes",
    "iter_vertical_eval_factories",
    "iter_vertical_pack_roots",
    "iter_vertical_web_roots",
    "get_vertical_spec",
    "is_vertical_registered",
    "registered_vertical_names",
    "vertical_load_errors",
    "clear_registry",
]
