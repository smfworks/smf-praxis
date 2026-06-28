"""Vertical packs — shareable, installable bundles that configure Praxis for a
domain (legal, medical, ...). A pack is a directory with a ``pack.json`` manifest:

    {
      "name": "legal",
      "version": "0.1.0",
      "vertical": "Legal",
      "description": "...",
      "systemPrompt": "You are a legal assistant ...",
      "complianceMode": "enforced",            # enforced/autonomous/permissive
      "tools": ["read_email", "draft_email"],  # allowlist subset; absent = all
      "riskPolicy": {                           # broker policy overrides
        "dualApprovalRisks": ["destructive", "send"],
        "autonomousRisks": ["read"],
        "egressCheck": true, "injectionCheck": true,
        "approvalTtlSeconds": 1800
      },
      "skills": [...], "knowledge": [...], "theme": {...}, "model": "..."
    }

p07 applies the persona (system prompt), compliance mode, risk-policy overrides,
and the tool allowlist. Skills / knowledge / theme / evals are carried in the
manifest and wired up by later roadmap items (p08-p11).
"""
from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg

MANIFEST = "pack.json"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")
_RISK_KEYS = {"read", "draft", "send", "destructive"}


@dataclass
class VerticalPack:
    name: str
    version: str = "0.1.0"
    description: str = ""
    vertical: str = ""
    system_prompt: str = ""
    compliance_mode: str | None = None
    tools: list = field(default_factory=list)
    risk_policy: dict = field(default_factory=dict)
    skills: list = field(default_factory=list)
    knowledge: list = field(default_factory=list)
    theme: dict = field(default_factory=dict)
    model: str | None = None
    path: str = ""

    @classmethod
    def from_manifest(cls, data: dict, path: str = "") -> "VerticalPack":
        return cls(
            name=str(data.get("name", "")),
            version=str(data.get("version", "0.1.0")),
            description=str(data.get("description", "")),
            vertical=str(data.get("vertical", "")),
            system_prompt=str(data.get("systemPrompt", "")),
            compliance_mode=(data.get("complianceMode") or None),
            tools=list(data.get("tools", []) or []),
            risk_policy=dict(data.get("riskPolicy", {}) or {}),
            skills=list(data.get("skills", []) or []),
            knowledge=list(data.get("knowledge", []) or []),
            theme=dict(data.get("theme", {}) or {}),
            model=(data.get("model") or None),
            path=path,
        )

    def to_manifest(self) -> dict:
        out: dict = {"name": self.name, "version": self.version}
        for key, val in (("description", self.description), ("vertical", self.vertical),
                         ("systemPrompt", self.system_prompt),
                         ("complianceMode", self.compliance_mode), ("tools", self.tools),
                         ("riskPolicy", self.risk_policy), ("skills", self.skills),
                         ("knowledge", self.knowledge), ("theme", self.theme),
                         ("model", self.model)):
            if val:
                out[key] = val
        return out


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


# --- discovery ---------------------------------------------------------------
def packs_dir() -> Path:
    return cfg.home_dir() / "packs"


def bundled_packs_dir() -> Path:
    return Path(__file__).resolve().parent / "packs"


def _load_dir(d: Path) -> "VerticalPack | None":
    mf = d / MANIFEST
    if not mf.is_file():
        return None
    try:
        data = json.loads(mf.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not data.get("name"):
        data["name"] = d.name
    return VerticalPack.from_manifest(data, path=str(d))


def list_packs() -> "dict[str, VerticalPack]":
    """All discoverable packs by name (user packs override bundled)."""
    found: dict[str, VerticalPack] = {}
    for base in (bundled_packs_dir(), packs_dir()):
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if d.is_dir():
                p = _load_dir(d)
                if p and p.name:
                    found[p.name] = p
    return found


def load_pack(name_or_path: str) -> "VerticalPack | None":
    """Load a pack by name (user dir, then bundled) or by directory path."""
    p = Path(name_or_path)
    if p.is_dir():
        return _load_dir(p)
    for base in (packs_dir(), bundled_packs_dir()):
        cand = base / name_or_path
        if cand.is_dir():
            return _load_dir(cand)
    return None


# --- lifecycle ---------------------------------------------------------------
def create_pack(name: str, system_prompt: str = "", vertical: str = "",
                description: str = "") -> Path:
    """Scaffold a new pack under the user packs dir. Returns its directory."""
    if not valid_name(name):
        raise ValueError(f"invalid pack name '{name}' (use lowercase a-z 0-9 _ -)")
    d = packs_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    pk = VerticalPack(
        name=name, vertical=vertical or name.title(), description=description,
        system_prompt=system_prompt
        or f"You are Praxis configured for the {vertical or name} vertical.")
    (d / MANIFEST).write_text(json.dumps(pk.to_manifest(), indent=2), encoding="utf-8")
    return d


def install_pack(src: str) -> "VerticalPack":
    """Copy a pack directory into the user packs dir (validating the manifest)."""
    src_dir = Path(src)
    pk = _load_dir(src_dir)
    if pk is None:
        raise ValueError(f"no {MANIFEST} found in '{src}'")
    if not valid_name(pk.name):
        raise ValueError(f"invalid pack name '{pk.name}'")
    dest = packs_dir() / pk.name
    dest.mkdir(parents=True, exist_ok=True)
    if src_dir.resolve() != dest.resolve():
        shutil.copytree(src_dir, dest, dirs_exist_ok=True)
    loaded = _load_dir(dest)
    assert loaded is not None
    return loaded


def activate(name: str) -> "VerticalPack":
    """Set a pack active: persist the pointer and apply its compliance mode."""
    pk = load_pack(name)
    if pk is None:
        raise ValueError(f"unknown pack '{name}'")
    cfg.set_active_pack_name(pk.name)
    if pk.compliance_mode:
        try:
            from .persistence import Store
            Store.open().set_compliance_mode(pk.compliance_mode)
        except Exception:
            pass
    return pk


def deactivate() -> None:
    cfg.set_active_pack_name(None)


def active() -> "VerticalPack | None":
    name = cfg.get_active_pack_name()
    return load_pack(name) if name else None


# --- application -------------------------------------------------------------
def _risk_set(values):
    from .broker import RiskClass
    out = set()
    for v in values or []:
        if str(v) in _RISK_KEYS:
            out.add(RiskClass(str(v)))
    return out


def apply_to_policy(pk: "VerticalPack", policy) -> None:
    """Apply a pack's tool allowlist + risk-policy overrides to a GovernancePolicy."""
    rp = pk.risk_policy or {}
    if "dualApprovalRisks" in rp:
        policy.dual_approval_risks = _risk_set(rp["dualApprovalRisks"])
    if "autonomousRisks" in rp:
        policy.autonomous_risks = _risk_set(rp["autonomousRisks"])
    if "egressCheck" in rp:
        policy.egress_check = bool(rp["egressCheck"])
    if "injectionCheck" in rp:
        policy.injection_check = bool(rp["injectionCheck"])
    if "approvalTtlSeconds" in rp:
        try:
            policy.approval_ttl_seconds = float(rp["approvalTtlSeconds"]) or None
        except (TypeError, ValueError):
            pass
    policy.pack_tools = set(pk.tools) if pk.tools else None


def apply_active_to_broker(broker) -> "VerticalPack | None":
    """Apply the active pack (if any) to a broker's policy. Never raises."""
    try:
        pk = active()
        if pk is not None:
            apply_to_policy(pk, broker.policy)
        return pk
    except Exception:
        return None


def compose_system(base: str) -> str:
    """Prepend the active pack's persona to a base system prompt."""
    try:
        pk = active()
        if pk and pk.system_prompt:
            return f"{pk.system_prompt}\n\n{base}"
    except Exception:
        pass
    return base
