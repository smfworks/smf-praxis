"""Plugin marketplace — publish, search, install plugins (Phase D / G9 marketplace).

Builds the distribution layer on top of the plugin contract (`plugins.py`). A
*published* plugin is a manifest + source module in a registry directory
(``~/.praxis/marketplace/`` by default, or a shared/team path via
``agents.marketplace.registry``). Installing copies the module into
``~/.praxis/plugins/`` after a fresh security scan, then it's enabled like any
plugin.

This is intentionally a **local/file-based** registry (dependency-free, works
offline, team-shareable via a synced folder or git). The same manifest +
``register(registry)`` contract is what a future networked registry would serve,
so nothing here is throwaway.

Safety: a plugin is **scanned on publish and again on install** (defense in
depth), and installed-but-disabled by default — installing never auto-runs code.
"""
from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.marketplace")


def registry_dir() -> Path:
    block = cfg.load_config().get("agents", {}).get("marketplace", {}) or {}
    custom = block.get("registry")
    return Path(custom) if custom else (Path(cfg.home_dir()) / "marketplace")


@dataclass
class Listing:
    name: str
    version: str
    description: str
    author: str
    published_ts: float
    grade: str = "A"

    def to_dict(self) -> dict:
        return {"name": self.name, "version": self.version,
                "description": self.description, "author": self.author,
                "published_ts": self.published_ts, "grade": self.grade}


def _manifest_path(name: str) -> Path:
    return registry_dir() / f"{name}.json"


def _version_tuple(v: str) -> tuple:
    """Parse a dotted version into a comparable int tuple; non-numeric parts -> 0."""
    out = []
    for part in str(v or "0").split("."):
        digits = "".join(c for c in part if c.isdigit())
        out.append(int(digits) if digits else 0)
    return tuple(out)


def _source_path(name: str) -> Path:
    return registry_dir() / f"{name}.py"


def publish(source_file: str, *, name: str = "", version: str = "0.1.0",
            description: str = "", author: str = "") -> dict:
    """Publish a plugin module into the registry after a security scan."""
    src = Path(source_file)
    if not src.exists():
        return {"error": f"source file not found: {source_file}"}
    name = name or src.stem
    code = src.read_text(encoding="utf-8", errors="replace")
    from .security_scan import scan_text
    rep = scan_text(code, target=f"plugin:{name}")
    if not rep.clean:
        return {"error": f"refused to publish '{name}': security scan "
                         f"{rep.grade} ({rep.summary()})"}
    # a publishable plugin must expose register(registry)
    if "def register(" not in code:
        return {"error": f"'{name}' has no register(registry) function"}
    # Refuse a version downgrade/re-publish over a newer one (basic integrity:
    # a published name shouldn't silently regress to an older version).
    existing = _manifest_path(name)
    if existing.exists():
        try:
            prev = json.loads(existing.read_text()).get("version", "0")
            if _version_tuple(version) < _version_tuple(prev):
                return {"error": f"refused to publish '{name}' v{version}: "
                                 f"older than published v{prev}"}
        except Exception:  # noqa: BLE001
            pass
    rd = registry_dir()
    rd.mkdir(parents=True, exist_ok=True)
    _source_path(name).write_text(code, encoding="utf-8")
    listing = Listing(name=name, version=version, description=description,
                      author=author, published_ts=time.time(), grade=rep.grade)
    _manifest_path(name).write_text(json.dumps(listing.to_dict(), indent=2))
    _log.info("published plugin '%s' v%s (grade %s)", name, version, rep.grade)
    return {"published": name, "version": version, "grade": rep.grade}


def search(query: str = "") -> list[Listing]:
    """List/search published plugins by name or description substring."""
    rd = registry_dir()
    if not rd.exists():
        return []
    out: list[Listing] = []
    q = (query or "").lower()
    for mf in sorted(rd.glob("*.json")):
        try:
            d = json.loads(mf.read_text())
        except Exception:  # noqa: BLE001
            continue
        listing = Listing(**{k: d.get(k) for k in
                             ("name", "version", "description", "author",
                              "published_ts", "grade")})
        if not q or q in listing.name.lower() or q in (listing.description or "").lower():
            out.append(listing)
    return out


def install(name: str, *, enable: bool = False) -> dict:
    """Install a published plugin: re-scan, copy into the plugins dir, optionally
    enable. Installed-but-disabled by default — install never auto-runs code."""
    src = _source_path(name)
    if not src.exists():
        return {"error": f"no published plugin '{name}' in {registry_dir()}"}
    code = src.read_text(encoding="utf-8", errors="replace")
    from .security_scan import scan_text
    rep = scan_text(code, target=f"plugin:{name}")
    if not rep.clean:
        return {"error": f"refused to install '{name}': security scan "
                         f"{rep.grade} ({rep.summary()})"}
    from .plugins import plugins_dir, set_enabled
    pdir = plugins_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, pdir / f"{name}.py")
    result = {"installed": name, "grade": rep.grade, "enabled": False}
    if enable:
        set_enabled(name, True)
        result["enabled"] = True
    _log.info("installed plugin '%s' (grade %s, enabled=%s)",
              name, rep.grade, result["enabled"])
    return result


def uninstall(name: str) -> dict:
    """Remove an installed plugin module and disable it."""
    from .plugins import plugins_dir, set_enabled
    p = plugins_dir() / f"{name}.py"
    existed = p.exists()
    if existed:
        p.unlink()
    set_enabled(name, False)
    return {"uninstalled": name, "removed": existed}
