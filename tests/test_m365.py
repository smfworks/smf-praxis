from hybridagent.broker import RiskClass
from hybridagent.m365_tools import m365_registry, build_m365_agent, M365Planner


class FakeBroker:
    """In-memory stand-in for the broker HTTP client (no network)."""

    def __init__(self, security=None):
        self.calls = []
        self.approvals = []
        self.last_draft_id = None
        self._security = security

    def execute(self, tool, args=None, approval_id=None):
        self.calls.append((tool, args or {}, approval_id))
        result = {"ok": True, "outcome": "success", "result": {}}
        if tool == "create_email_draft":
            result["result"] = {"draftId": "d-123"}
        if self._security and tool == "search_mail":
            result["security"] = self._security
        return result

    def approve(self, tool, args=None):
        self.approvals.append((tool, args or {}))
        return {"ok": True, "approvalId": "appr-xyz"}


def test_risk_class_mapping():
    reg = m365_registry(FakeBroker())
    assert reg.get("search_mail").risk is RiskClass.READ
    assert reg.get("create_email_draft").risk is RiskClass.DRAFT
    assert reg.get("send_approved_draft").risk is RiskClass.SEND
    assert reg.get("delete_file").risk is RiskClass.DESTRUCTIVE


def test_reads_and_drafts_run_autonomously():
    client = FakeBroker()
    agent, _ = build_m365_agent(client)
    report = agent.handle("Review recent mail and gather context")
    assert any(a.startswith("[read]") for a in report.actions)
    # m365_status + list_today_events + search_mail were executed on the broker
    executed = {c[0] for c in client.calls}
    assert {"m365_status", "list_today_events", "search_mail"} <= executed
    assert report.pending_approvals == []


def test_send_is_held_then_mints_broker_approval_on_approve():
    client = FakeBroker()
    agent, _ = build_m365_agent(client)
    report = agent.handle("Prepare a customer follow-up email and send it")
    # draft happened autonomously; send is held
    assert any(c[0] == "create_email_draft" for c in client.calls)
    assert len(report.pending_approvals) == 1
    assert report.pending_approvals[0]["tool"] == "send_approved_draft"
    assert client.approvals == []                     # nothing minted yet

    out = agent.approve(report.pending_approvals[0]["approval_id"])
    assert "success" in out
    # On approval, Praxis minted the broker approval AND executed with the token.
    assert client.approvals == [("send_approved_draft", {"draftId": "d-123"})]
    sent = [c for c in client.calls if c[0] == "send_approved_draft"]
    assert sent and sent[-1][2] == "appr-xyz"         # executed with approvalId


def test_firewall_findings_surface_in_action_text():
    client = FakeBroker(security={"risk": "high",
                                  "findings": [{"id": "ignore_previous"}]})
    agent, _ = build_m365_agent(client)
    report = agent.handle("Review recent mail")
    assert any("firewall:high" in a for a in report.actions)


def test_planner_only_emits_known_broker_tools():
    reg = m365_registry(FakeBroker())
    plan = M365Planner(reg).plan("Prepare a follow-up email and delete obsolete file")
    for step in plan.steps:
        assert reg.get(step.tool) is not None
