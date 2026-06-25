"""Wire Praxis to the OpenClaw M365 Access Broker.

Maps the broker's catalog to Praxis tools + risk classes so the existing
governance loop applies end-to-end:

    broker sensitivity   ->  Praxis RiskClass     ->  Praxis behavior
    read                     READ                     autonomous
    write (create draft)     DRAFT                    autonomous (never sends)
    outbound (send/share)    SEND                     held for human approval
    destructive (delete)     DESTRUCTIVE              held for human approval

When the human approves a held action in the Praxis CLI/TUI, Praxis acts as the
host UI: it calls the broker's /approve (approver key) to mint a single-use,
tool-scoped token, then /execute with that token. The agent key alone can never
send/share/delete.
"""
from __future__ import annotations

from .agent import PraxisAgent
from .broker import RiskClass
from .broker_client import BrokerClient
from .planner import Plan, Planner, Step
from .tools import Tool, ToolRegistry

# broker catalog sensitivity -> Praxis risk class
_RISK = {
    "m365_status": RiskClass.READ,
    "list_today_events": RiskClass.READ,
    "search_mail": RiskClass.READ,
    "get_mail": RiskClass.READ,
    "search_files": RiskClass.READ,
    "get_file_text": RiskClass.READ,
    "create_email_draft": RiskClass.DRAFT,
    "send_approved_draft": RiskClass.SEND,
    "share_file": RiskClass.SEND,
    "delete_file": RiskClass.DESTRUCTIVE,
}


def _fmt(name: str, r: dict) -> str:
    if not r.get("ok"):
        why = r.get("reasons") or r.get("error") or "denied"
        return f"{name}: broker refused ({why})"
    out = f"{name}: {r.get('outcome', 'ok')}"
    sec = r.get("security")
    if sec and sec.get("risk") not in (None, "none"):
        ids = [f.get("id") for f in sec.get("findings", [])]
        out += f"  [firewall:{sec['risk']} {ids}]"
    if r.get("blocked"):
        out += " [QUARANTINED — evidence only]"
    return out


def m365_registry(client: BrokerClient) -> ToolRegistry:
    """Build a Praxis ToolRegistry backed by the broker."""
    reg = ToolRegistry()

    def read_tool(name: str):
        def run(**args) -> str:
            return _fmt(name, client.execute(name, args))
        return Tool(name, RiskClass.READ, f"broker:{name}", run)

    for name in ("m365_status", "list_today_events", "search_mail",
                 "get_mail", "search_files", "get_file_text"):
        reg.register(read_tool(name))

    def draft_run(to=None, subject="", body="", **_) -> str:
        r = client.execute("create_email_draft", {
            "to": to or ["recipient@example.com"],
            "subject": subject or "Draft",
            "body": body or "(draft body)",
        })
        did = (r.get("result") or {}).get("draftId")
        if did:
            client.last_draft_id = did
        return _fmt("create_email_draft", r)

    reg.register(Tool("create_email_draft", RiskClass.DRAFT,
                      "broker:create_email_draft (never sends)", draft_run))

    # Consequential tools: Praxis holds them; on approval it mints the broker
    # token (approver key) then executes.
    def send_run(draftId=None, **_) -> str:
        did = draftId or client.last_draft_id or "draft-pending"
        appr = client.approve("send_approved_draft", {"draftId": did})
        if not appr.get("ok"):
            return _fmt("send_approved_draft", appr)
        return _fmt("send_approved_draft",
                    client.execute("send_approved_draft", {"draftId": did},
                                   approval_id=appr.get("approvalId")))

    reg.register(Tool("send_approved_draft", RiskClass.SEND,
                      "broker:send_approved_draft (approval)", send_run))

    def share_run(id="", recipients=None, **_) -> str:
        args = {"id": id or "file-1", "recipients": recipients or ["someone@example.com"]}
        appr = client.approve("share_file", args)
        if not appr.get("ok"):
            return _fmt("share_file", appr)
        return _fmt("share_file",
                    client.execute("share_file", args, approval_id=appr.get("approvalId")))

    reg.register(Tool("share_file", RiskClass.SEND, "broker:share_file (approval)", share_run))

    def delete_run(id="", **_) -> str:
        args = {"id": id or "obsolete.txt"}
        appr = client.approve("delete_file", args)
        if not appr.get("ok"):
            return _fmt("delete_file", appr)
        return _fmt("delete_file",
                    client.execute("delete_file", args, approval_id=appr.get("approvalId")))

    reg.register(Tool("delete_file", RiskClass.DESTRUCTIVE,
                      "broker:delete_file (approval)", delete_run))
    return reg


class M365Planner(Planner):
    """Planner that emits broker-tool-bound steps with valid Graph args."""

    def read_tools_for(self, goal: str) -> list[str]:
        tools = ["list_today_events", "search_mail"]
        if any(k in goal.lower() for k in ("file", "doc", "report", "project")):
            tools.append("search_files")
        return tools

    def plan(self, goal: str) -> Plan:
        g = goal.lower()
        steps: list[Step] = [
            Step("check signed-in identity", "m365_status"),
            Step("gather calendar context", "list_today_events"),
            Step("search related mail", "search_mail", {"query": goal}),
        ]
        if any(k in g for k in ("file", "doc", "report", "project")):
            steps.append(Step("search related files", "search_files", {"query": goal}))
        if any(k in g for k in ("follow up", "follow-up", "reply", "email", "respond")):
            steps.append(Step("draft follow-up email", "create_email_draft", {
                "to": ["customer@example.com"], "subject": f"Re: {goal}",
                "body": "Draft grounded in gathered context.",
            }))
            steps.append(Step("send the drafted email", "send_approved_draft", {}))
        if any(k in g for k in ("delete", "remove", "clean up")):
            steps.append(Step("delete obsolete file", "delete_file", {"id": "obsolete.txt"}))
        return Plan(goal=goal, steps=steps)


def build_m365_agent(client: BrokerClient | None = None,
                     store=None) -> tuple[PraxisAgent, BrokerClient]:
    """A PraxisAgent whose tools are backed by the M365 broker."""
    client = client or BrokerClient.from_env()
    reg = m365_registry(client)
    agent = PraxisAgent(registry=reg, store=store)
    agent.planner = M365Planner(reg, agent.llm)
    return agent, client
