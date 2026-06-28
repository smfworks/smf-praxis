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

