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
and the tool allowlist; p10 ingests the manifest's ``knowledge`` files into a
``pack:<name>`` RAG namespace on activation and grounds chat answers in them; p11
installs the manifest's ``skills`` (inline or SKILL.md refs) into the shared skill
library. Theme / model are carried in the manifest for later roadmap items.
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
    """Scaffold a new pack under the user packs dir. Returns its directory.

    When ``vertical`` (or ``name``) matches a built-in template, the new pack is
    seeded with that template's persona, compliance mode, and risk policy; explicit
    arguments still win.
    """
    if not valid_name(name):
        raise ValueError(f"invalid pack name '{name}' (use lowercase a-z 0-9 _ -)")
    from . import vertical_templates as vt
    tmpl = vt.get_template(vertical) or vt.get_template(name) or {}
    d = packs_dir() / name
    d.mkdir(parents=True, exist_ok=True)
    pk = VerticalPack(
        name=name,
        vertical=vertical or tmpl.get("vertical", "") or name.title(),
        description=description or tmpl.get("description", ""),
        system_prompt=(
            system_prompt or tmpl.get("systemPrompt", "")
            or f"You are Praxis configured for the {vertical or name} vertical."),
        compliance_mode=tmpl.get("complianceMode"),
        risk_policy=dict(tmpl.get("riskPolicy", {}) or {}),
    )
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


def activate(name: str, store=None) -> "VerticalPack":
    """Set a pack active: persist the pointer, apply its compliance mode, and
    ingest any bundled knowledge into the pack's namespace (best-effort)."""
    pk = load_pack(name)
    if pk is None:
        raise ValueError(f"unknown pack '{name}'")
    cfg.set_active_pack_name(pk.name)
    if pk.compliance_mode:
        try:
            from .persistence import Store
            (store or Store.open()).set_compliance_mode(pk.compliance_mode)
        except Exception:
            pass
    if pk.knowledge:
        try:
            ingest_knowledge(pk, store)
        except Exception:
            pass
    if pk.skills:
        try:
            install_skills(pk, store)
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


def pack_ns(name: str) -> str:
    """The RAG namespace that isolates a pack's bundled knowledge."""
    return f"pack:{name}"


def ingest_knowledge(pk: "VerticalPack", store=None) -> int:
    """Ingest a pack's ``knowledge`` files into ``pack:<name>`` (idempotent).

    Each entry is a path relative to the pack directory. Text/markdown are read
    directly; other formats fall back to the document extractor. Returns the
    total chunk count. Never raises if RAG/embeddings are unavailable.
    """
    if not pk.knowledge or not pk.path:
        return 0
    from .persistence import Store
    from .rag import Rag
    store = store or Store.open()
    rag = Rag(store, ns=pack_ns(pk.name))
    base = Path(pk.path)
    total = 0
    for entry in pk.knowledge:
        fp = base / str(entry)
        if not fp.is_file():
            continue
        if fp.suffix.lower() in (".md", ".txt"):
            total += rag.ingest_text(fp.read_text(encoding="utf-8"), source=fp.name,
                                     provenance=f"pack:{pk.name}:{fp.name}")
        else:
            total += rag.ingest_file(fp)[1]
    return total


def knowledge_chunks(query: str, store=None, k: int = 4) -> list:
    """Retrieve the active pack's knowledge chunks for ``query`` ([] if none)."""
    try:
        pk = active()
        if pk is None or not pk.knowledge:
            return []
        from .rag import Rag
        return Rag(store, ns=pack_ns(pk.name)).retrieve(query, k=k)
    except Exception:
        return []


def install_skills(pk: "VerticalPack", store=None) -> int:
    """Install a pack's skills into the shared SkillLibrary (idempotent).

    Each ``skills`` entry is an inline dict (``name``/``trigger``/``body``) or a
    path (relative to the pack dir) to a ``SKILL.md`` file. Skills are tagged with
    the pack provenance so they ground perception/chat retrieval. Returns the
    count installed. Never raises if RAG/embeddings are unavailable.
    """
    if not pk.skills:
        return 0
    from .persistence import Store
    from .skills import Skill, SkillLibrary
    lib = SkillLibrary(store=store or Store.open())
    base = Path(pk.path) if pk.path else None
    count = 0
    for entry in pk.skills:
        sk: "Skill | None" = None
        if isinstance(entry, dict) and entry.get("name"):
            sk = Skill(name=str(entry["name"]), trigger=str(entry.get("trigger", "")),
                       body=str(entry.get("body", "")), provenance=f"pack:{pk.name}")
        elif isinstance(entry, str) and base is not None and (base / entry).is_file():
            sk = Skill.from_markdown((base / entry).read_text(encoding="utf-8"))
            sk.provenance = f"pack:{pk.name}"
        if sk:
            lib.add(sk)
            count += 1
    return count


def compose_system(base: str) -> str:
    """Prepend the active pack's persona to a base system prompt."""
    try:
        pk = active()
        if pk and pk.system_prompt:
            return f"{pk.system_prompt}\n\n{base}"
    except Exception:
        pass
    return base
