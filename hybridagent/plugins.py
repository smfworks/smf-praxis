"""Plugin system — third-party extension of Praxis (Phase D / G9).

A plugin is a single ``.py`` module dropped in ``~/.praxis/plugins/`` that exposes
a top-level ``register(registry)`` function. It registers ordinary governed
:class:`~hybridagent.tools.Tool` objects, so every plugin tool flows through the
same broker (risk classes, approvals, audit) as a built-in — there is no
privileged plugin path.

Safety (third-party code is untrusted):
* a plugin is loaded only if it is **enabled** (``agents.plugins.enabled`` list),
  so dropping a file in the directory is not enough to run it;
* the module source is **security-scanned** before import; a plugin whose source
  trips a critical rule is skipped;
* tools a plugin registers are scanned the same way skills/MCP tools are, and a
  plugin tool's risk class is respected by the broker.

Stdlib only (``importlib``). This is the foundation the marketplace builds on:
the same ``register(registry)`` contract is what a published package would ship.
"""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.plugins")


def plugins_dir() -> Path:
    d = Path(cfg.home_dir()) / "plugins"
    return d


@dataclass
class PluginInfo:
    name: str
    path: str
    enabled: bool
    loaded: bool = False
    tools: list[str] = field(default_factory=list)
    error: str = ""


def _config() -> dict:
    return cfg.load_config().get("agents", {}).get("plugins", {}) or {}


def _enabled_set() -> set[str]:
    return set(_config().get("enabled", []) or [])


def discover() -> list[Path]:
    """All candidate plugin modules (``*.py``, excluding dunder files)."""
    d = plugins_dir()
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.py") if not p.name.startswith("_"))


def _scan_source(path: Path) -> tuple[bool, str]:
    """Security-scan a plugin's source before importing it."""
    try:
        from .security_scan import scan_text
        rep = scan_text(path.read_text(encoding="utf-8", errors="replace"),
                        target=f"plugin:{path.stem}")
        return rep.clean, ("" if rep.clean else rep.summary())
    except Exception as exc:  # noqa: BLE001
        return False, f"scan error: {exc}"


def _load_module(path: Path):
    spec = importlib.util.spec_from_file_location(f"praxis_plugin_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_plugins(registry, *, enabled_only: bool = True) -> list[PluginInfo]:
    """Discover, scan, import, and register enabled plugins into ``registry``.

    Returns one PluginInfo per discovered plugin (loaded or skipped, with reason).
    A plugin that fails any stage is skipped without aborting the others, so one
    bad plugin can't break startup.
    """
    enabled = _enabled_set()
    infos: list[PluginInfo] = []
    for path in discover():
        name = path.stem
        is_on = name in enabled
        info = PluginInfo(name=name, path=str(path), enabled=is_on)
        if enabled_only and not is_on:
            infos.append(info)
            continue
        clean, detail = _scan_source(path)
        if not clean:
            info.error = f"security scan failed: {detail}"
            _log.warning("skipping plugin %s: %s", name, info.error)
            infos.append(info)
            continue
        try:
            mod = _load_module(path)
            reg_fn = getattr(mod, "register", None)
            if not callable(reg_fn):
                info.error = "no register(registry) function"
                infos.append(info)
                continue
            before = set(registry.names())
            reg_fn(registry)
            info.tools = sorted(set(registry.names()) - before)
            info.loaded = True
            _log.info("loaded plugin %s (+%d tools)", name, len(info.tools))
        except Exception as exc:  # noqa: BLE001 - one bad plugin can't break others
            info.error = f"load error: {exc}"
            _log.warning("plugin %s failed to load: %s", name, exc)
        infos.append(info)
    return infos


def set_enabled(name: str, enabled: bool) -> dict:
    """Add/remove a plugin from the enabled list in config."""
    conf = cfg.load_config()
    block = conf.setdefault("agents", {}).setdefault("plugins", {})
    lst = set(block.get("enabled", []) or [])
    if enabled:
        lst.add(name)
    else:
        lst.discard(name)
    block["enabled"] = sorted(lst)
    cfg.save_config(conf)
    return {"plugin": name, "enabled": enabled}


def list_plugins() -> list[PluginInfo]:
    """Discovery view without importing anything (safe to call anytime)."""
    enabled = _enabled_set()
    return [PluginInfo(name=p.stem, path=str(p), enabled=p.stem in enabled)
            for p in discover()]
