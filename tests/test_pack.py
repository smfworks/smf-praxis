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
