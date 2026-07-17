"""Tests for vertical packs: schema, discovery, activation, and policy application."""

import json

import pytest

from hybridagent import config as cfg
from hybridagent import pack
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_bundled_general_pack_discoverable(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    packs = pack.list_packs()
    assert "general" in packs
    assert packs["general"].compliance_mode == "enforced"


def test_create_and_load_roundtrip(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    pack.create_pack("legal", vertical="Legal", description="legal helper")
    p = pack.load_pack("legal")
    assert p is not None and p.vertical == "Legal"
    assert p.system_prompt


def test_invalid_name_rejected(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    with pytest.raises(ValueError):
        pack.create_pack("Bad Name!")


def test_activate_sets_pointer_and_compliance(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    pack.create_pack("med")
    d = pack.packs_dir() / "med"
    (d / "pack.json").write_text(json.dumps({
        "name": "med", "vertical": "Medical",
        "systemPrompt": "You are a clinical assistant.",
        "complianceMode": "enforced",
        "tools": ["read_record", "draft_note"],
        "riskPolicy": {"dualApprovalRisks": ["destructive", "send"]},
    }), encoding="utf-8")
    p = pack.activate("med")
    assert cfg.get_active_pack_name() == "med"
    assert p.compliance_mode == "enforced"
    assert pack.active().name == "med"
    pack.deactivate()
    assert cfg.get_active_pack_name() is None
    assert pack.active() is None


def test_install_from_path(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    src = tmp_path / "src-pack"
    src.mkdir()
    (src / "pack.json").write_text(json.dumps({"name": "research", "vertical": "Research"}),
                                   encoding="utf-8")
    p = pack.install_pack(str(src))
    assert p.name == "research"
    assert (pack.packs_dir() / "research" / "pack.json").is_file()


def test_apply_to_policy_overrides(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.VerticalPack(
        name="x", tools=["read_record", "draft_note"],
        risk_policy={"dualApprovalRisks": ["destructive", "send"],
                     "autonomousRisks": ["read"], "egressCheck": True,
                     "approvalTtlSeconds": 1800})
    policy = GovernancePolicy(allowed_tools={"read_record", "draft_note", "send_email"})
    pack.apply_to_policy(p, policy)
    assert policy.dual_approval_risks == {RiskClass.DESTRUCTIVE, RiskClass.SEND}
    assert policy.autonomous_risks == {RiskClass.READ}
    assert policy.approval_ttl_seconds == 1800
    assert policy.pack_tools == {"read_record", "draft_note"}


def test_pack_tool_allowlist_enforced_by_broker(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    policy = GovernancePolicy(allowed_tools={"read_record", "send_email"},
                              pack_tools={"read_record"})
    b = GovernanceBroker(policy)
    # send_email is allowlisted but NOT enabled by the pack -> pack_restricted deny.
    d = b.authorize("agent", "send_email", RiskClass.SEND, {})
    assert d.verdict is Verdict.DENY and d.policy_rule == "pack_restricted"
    # read_record is enabled by the pack -> allowed (read is autonomous).
    d2 = b.authorize("agent", "read_record", RiskClass.READ, {})
    assert d2.verdict is Verdict.ALLOW


def test_no_pack_tools_means_no_restriction(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    policy = GovernancePolicy(allowed_tools={"read_record"})  # pack_tools defaults None
    b = GovernanceBroker(policy)
    assert b.authorize("agent", "read_record", RiskClass.READ, {}).verdict is Verdict.ALLOW


def test_compose_system_prepends_active_persona(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    pack.create_pack("x", system_prompt="PERSONA-LINE")
    pack.activate("x")
    out = pack.compose_system("BASE-LINE")
    assert "PERSONA-LINE" in out and "BASE-LINE" in out
    assert out.index("PERSONA-LINE") < out.index("BASE-LINE")


def test_apply_active_to_broker_noop_without_active(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    b = GovernanceBroker(GovernancePolicy(allowed_tools={"a"}))
    assert pack.apply_active_to_broker(b) is None
    assert b.policy.pack_tools is None


# --- p08: per-vertical templates ---------------------------------------------
def test_templates_cover_core_verticals():
    from hybridagent import vertical_templates as vt
    names = set(vt.list_templates())
    assert {"general", "legal", "medical", "forensic",
            "education", "homeschool", "business", "developer"} <= names


def test_get_template_is_case_insensitive_and_alias_aware():
    from hybridagent import vertical_templates as vt
    assert vt.get_template("Legal")["vertical"] == "Legal"
    assert vt.get_template("lawyer")["vertical"] == "Legal"      # alias
    assert vt.get_template("dental")["vertical"] == "Medical/Dental"
    assert vt.get_template("coding")["vertical"] == "Developer"
    assert vt.get_template("zzz-unknown") is None
    assert vt.get_template("") is None


def test_create_from_legal_template_seeds_policy(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    pack.create_pack("matter1", vertical="legal")
    p = pack.load_pack("matter1")
    assert p is not None
    assert p.compliance_mode == "enforced"
    assert "legal" in p.system_prompt.lower()
    assert set(p.risk_policy["dualApprovalRisks"]) == {"send", "destructive"}
    assert p.risk_policy["egressCheck"] is True
    # the seeded risk policy flows through to a GovernancePolicy
    policy = GovernancePolicy(allowed_tools={"x"})
    pack.apply_to_policy(p, policy)
    assert policy.dual_approval_risks == {RiskClass.SEND, RiskClass.DESTRUCTIVE}


def test_create_unknown_vertical_is_generic(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    pack.create_pack("plain", vertical="nonesuch")
    p = pack.load_pack("plain")
    assert p is not None
    assert p.compliance_mode is None
    assert p.risk_policy == {}


def test_explicit_args_override_template(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    pack.create_pack("custom", vertical="legal", system_prompt="MY-OWN-PERSONA",
                     description="mine")
    p = pack.load_pack("custom")
    assert p.system_prompt == "MY-OWN-PERSONA"
    assert p.description == "mine"
    assert p.compliance_mode == "enforced"  # still seeded from the template


def test_homeschool_template_aliases_and_posture():
    from hybridagent import vertical_templates as vt
    assert "homeschool" in vt.list_templates()
    for alias in ("homeschooling", "home-school", "k12", "parent-educator"):
        assert vt.get_template(alias)["vertical"] == "Homeschool"
    t = vt.get_template("homeschool")
    assert t["complianceMode"] == "autonomous"
    assert set(t["riskPolicy"]["dualApprovalRisks"]) == {"send", "destructive"}
    assert set(t["riskPolicy"]["autonomousRisks"]) == {"read", "draft"}


def test_bundled_homeschool_pack_discoverable(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    packs = pack.list_packs()
    assert "homeschool" in packs
    hs = packs["homeschool"]
    assert hs.vertical == "Homeschool"
    assert hs.compliance_mode == "autonomous"
    assert "homeschool" in hs.system_prompt.lower()


def test_create_from_homeschool_template_seeds_pack(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    pack.create_pack("family", vertical="homeschooling")
    p = pack.load_pack("family")
    assert p is not None and p.compliance_mode == "autonomous"
    assert p.risk_policy["autonomousRisks"] == ["read", "draft"]


# --- p10: pack knowledge bundles ---------------------------------------------
def test_bundled_homeschool_pack_has_knowledge(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert "knowledge.md" in pack.list_packs()["homeschool"].knowledge


def test_activate_ingests_knowledge_and_scopes_retrieval(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("PRAXIS_EMBED", "mock")
    from hybridagent.persistence import Store
    d = pack.packs_dir() / "hs"
    d.mkdir(parents=True)
    (d / "kn.md").write_text("Homeschool attendance logs span 180 instructional days.",
                             encoding="utf-8")
    (d / "pack.json").write_text(json.dumps({
        "name": "hs", "knowledge": ["kn.md"], "complianceMode": "autonomous"}),
        encoding="utf-8")
    store = Store.open(tmp_path / "kb.db")
    n = pack.ingest_knowledge(pack.load_pack("hs"), store)
    assert n >= 1
    pack.activate("hs")
    hits = pack.knowledge_chunks("attendance instructional days", store, k=3)
    assert hits and "attendance" in hits[0].text.lower()
    assert store.count_vectors(pack.pack_ns("hs")) >= 1  # isolated namespace


def test_knowledge_chunks_empty_without_active_pack(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from hybridagent.persistence import Store
    assert pack.knowledge_chunks("anything", Store.open(tmp_path / "k.db")) == []


# --- p11: pack skills ---------------------------------------------------------
def test_bundled_homeschool_pack_has_skill():
    assert any(s.get("name") == "lesson-plan"
               for s in pack.list_packs()["homeschool"].skills)


# --- p12: pack model + theme --------------------------------------------------
def test_pack_pins_model_as_fallback(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    from hybridagent import config as c
    assert pack.resolve_model() is None
    pack.create_pack("p", system_prompt="x")
    d = pack.packs_dir() / "p"
    (d / "pack.json").write_text(json.dumps(
        {"name": "p", "model": "openai/gpt-4o"}), encoding="utf-8")
    pack.activate("p")
    assert pack.resolve_model() == "openai/gpt-4o"
    assert c.get_default_model() == "openai/gpt-4o"  # fallback when no explicit default
    pack.deactivate()
    assert pack.resolve_model() is None


def test_active_theme_surfaces_tokens(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    assert pack.active_theme() == {}
    d = pack.packs_dir() / "t"
    d.mkdir(parents=True)
    (d / "pack.json").write_text(json.dumps(
        {"name": "t", "theme": {"accent": "#0a7"}}), encoding="utf-8")
    pack.activate("t")
    assert pack.active_theme()["accent"] == "#0a7"


def test_install_skills_inline_and_retrievable(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setenv("PRAXIS_EMBED", "mock")
    from hybridagent.persistence import Store
    from hybridagent.skills import SkillLibrary
    pk = pack.VerticalPack(name="hs", path=str(tmp_path), skills=[
        {"name": "lesson-plan", "trigger": "planning a homeschool lesson",
         "body": "1. objectives 2. activity 3. log it"}])
    store = Store.open(tmp_path / "kb.db")
    assert pack.install_skills(pk, store) == 1
    hits = SkillLibrary(store=store).retrieve("plan a homeschool lesson", k=1)
    assert hits and hits[0].name == "lesson-plan"
    assert hits[0].provenance == "pack:hs"



# ======================================================================
# Law Firm pack (Slice 1) — the 13-state legal vertical pack
# ======================================================================
def test_law_firm_pack_discoverable(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    packs = pack.list_packs()
    assert "law_firm" in packs
    p = packs["law_firm"]
    assert p.vertical == "Law Firm"
    assert p.compliance_mode == "enforced"


def test_law_firm_persona_contains_guardrails(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("law_firm")
    assert p is not None
    # UPL guardrail — non-negotiable across all 13 states
    assert "do not provide legal advice" in p.system_prompt
    assert "unauthorized practice of law" in p.system_prompt
    # IOLTA / trust-accounting guardrail
    assert "IOLTA" in p.system_prompt
    assert "trust accounting" in p.system_prompt.lower()
    # the per-jurisdiction compliance flags
    assert "NY" in p.system_prompt and "FL" in p.system_prompt  # ad-filing states
    assert "MA" in p.system_prompt  # WISP
    assert "SHIELD" in p.system_prompt  # NY SHIELD
    assert "conflict" in p.system_prompt.lower()
    assert "litigation hold" in p.system_prompt.lower()


def test_law_firm_risk_policy_holds_send_and_destructive(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("law_firm")
    assert p is not None
    assert "send" in p.risk_policy["dualApprovalRisks"]
    assert "destructive" in p.risk_policy["dualApprovalRisks"]
    assert "read" in p.risk_policy["autonomousRisks"]
    assert "draft" in p.risk_policy["autonomousRisks"]
    # egress + injection guards on (regulated posture)
    assert p.risk_policy.get("egressCheck") is True
    assert p.risk_policy.get("injectionCheck") is True


def test_law_firm_tool_allowlist_includes_compliance_tools(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("law_firm")
    assert p is not None
    # the seven compliance-module tools must be allowed
    for t in ("conflict_check", "filing_track", "legal_hold_issue",
              "credential_status", "security_attest", "privilege_log",
              "expert_disclosure"):
        assert t in p.tools, f"{t} should be in the law_firm tool allowlist"
    # read/draft research tools
    assert "rag_search" in p.tools
    assert "draft_email" in p.tools


def test_law_firm_skills_present(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("law_firm")
    assert p is not None
    skill_names = [s["name"] for s in p.skills]
    assert "conflict-check" in skill_names
    assert "ad-filing-gate" in skill_names
    assert "matter-hold" in skill_names
    assert "ce-status" in skill_names
    # each skill has a trigger + body
    for s in p.skills:
        assert s.get("trigger") and s.get("body")


def test_law_firm_knowledge_file_present(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.load_pack("law_firm")
    assert p is not None
    assert "knowledge.md" in p.knowledge


def test_law_firm_template_in_vertical_templates():
    from hybridagent.vertical_templates import get_template, list_templates
    assert "law_firm" in list_templates()
    t = get_template("law_firm")
    assert t is not None
    assert t["vertical"] == "Law Firm"
    assert t["complianceMode"] == "enforced"
    # the template persona carries the same guardrails
    assert "do not provide legal advice" in t["systemPrompt"]
    assert "IOLTA" in t["systemPrompt"]


def test_law_firm_aliases_resolve():
    from hybridagent.vertical_templates import get_template
    for alias in ("lawfirm", "law-firm", "law_office", "law-office"):
        t = get_template(alias)
        assert t is not None and t["vertical"] == "Law Firm", (
            f"{alias} should resolve to Law Firm")


def test_law_firm_activate_sets_pointer_and_compliance(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    p = pack.activate("law_firm")
    assert p is not None
    from hybridagent import config as cfg
    assert cfg.get_active_pack_name() == "law_firm"
    assert p.compliance_mode == "enforced"
    # the risk policy applies to a broker
    from hybridagent.broker import GovernanceBroker, GovernancePolicy
    broker = GovernanceBroker(GovernancePolicy())
    applied = pack.apply_active_to_broker(broker)
    assert applied is not None
    assert applied.name == "law_firm"
    pack.deactivate()
    assert cfg.get_active_pack_name() is None


def test_law_firm_theme_exposed():
    from hybridagent.vertical_templates import get_template
    # the pack theme is on the pack, not the template; check via load_pack
    import hybridagent.config as cfg
    import os, pathlib
    # use the bundled pack dir directly (no home override needed for read)
    from hybridagent.pack import bundled_packs_dir, _load_dir
    p = _load_dir(bundled_packs_dir() / "law_firm")
    assert p is not None
    assert p.theme.get("accent") == "#1e3a8a"
    assert p.theme.get("panel") == "#0f172a"
