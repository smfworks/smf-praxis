import os
import threading

from hybridagent.broker import (
    GovernanceBroker,
    GovernancePolicy,
    RiskClass,
)
from hybridagent.chat_agent import GovernedChatAgent
from hybridagent.mcp_client import (
    MCPClient,
    StdioTransport,
    decode_message,
    echo_handler,
    encode_message,
    mcp_tools,
    risk_for_tool,
    serve_stdio,
)
from hybridagent.tools import ToolRegistry


# ------------------------------------------------------------------- codec
def test_codec_roundtrip():
    msg = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"x": 1}}
    line = encode_message(msg)
    assert line.endswith(b"\n") and b"\n" not in line[:-1]
    assert decode_message(line) == msg


def test_decode_blank_and_invalid():
    assert decode_message(b"   \n") is None
    assert decode_message(b"not json") is None
    assert decode_message(b"[1,2,3]") is None  # not an object


# ------------------------------------------------------------- risk mapping
def test_risk_from_annotations_and_name():
    # bare readOnlyHint on unrecognized name is untrusted → SEND
    assert risk_for_tool({"name": "x", "annotations": {"readOnlyHint": True}}) \
        is RiskClass.SEND
    assert risk_for_tool({"name": "x", "annotations": {"destructiveHint": True}}) \
        is RiskClass.DESTRUCTIVE
    assert risk_for_tool({"name": "delete_user"}) is RiskClass.DESTRUCTIVE
    assert risk_for_tool({"name": "send_email"}) is RiskClass.SEND
    assert risk_for_tool({"name": "create_note"}) is RiskClass.DRAFT
    assert risk_for_tool({"name": "get_status"}) is RiskClass.READ
    # readOnly + read-ish name still READ; delete name wins over readOnlyHint
    assert risk_for_tool({"name": "list_items",
                          "annotations": {"readOnlyHint": True}}) is RiskClass.READ
    assert risk_for_tool({"name": "delete_user",
                          "annotations": {"readOnlyHint": True}}) is RiskClass.DESTRUCTIVE


def test_risk_override_wins():
    assert risk_for_tool({"name": "delete_user"}, override="read") is RiskClass.READ


def test_unknown_external_tool_defaults_to_held_send():
    # No annotation and no recognizable verb -> SEND so the broker holds it,
    # rather than auto-executing an unclassifiable external tool.
    assert risk_for_tool({"name": "transfer_funds"}) is RiskClass.SEND
    assert risk_for_tool({"name": "frobnicate"}) is RiskClass.SEND
    # A read-ish verb is still auto-runnable for convenience.
    assert risk_for_tool({"name": "list_widgets"}) is RiskClass.READ


# ----------------------------------------------------- client (fake transport)
class _FakeTransport:
    def __init__(self):
        self.notified = []

    def request(self, method, params=None, timeout=20.0):
        if method == "initialize":
            return {"serverInfo": {"name": "fake"}}
        if method == "tools/list":
            return {"tools": [
                {"name": "echo", "description": "echo",
                 "inputSchema": {"type": "object"},
                 "annotations": {"readOnlyHint": True}},
                {"name": "delete_thing", "description": "del",
                 "annotations": {"destructiveHint": True}},
            ]}
        if method == "tools/call":
            if params["name"] == "boom":
                return {"content": [{"type": "text", "text": "bad"}], "isError": True}
            return {"content": [{"type": "text", "text": f"called {params['name']}"}]}
        return {}

    def notify(self, method, params=None):
        self.notified.append(method)

    def close(self):
        pass


def test_client_initialize_and_list():
    t = _FakeTransport()
    client = MCPClient(t)
    client.initialize()
    assert client.server_info["name"] == "fake"
    assert "notifications/initialized" in t.notified
    names = [td["name"] for td in client.list_tools()]
    assert names == ["echo", "delete_thing"]


def test_client_call_tool_joins_text_and_flags_errors():
    client = MCPClient(_FakeTransport())
    assert client.call_tool("echo", {"text": "hi"}) == "called echo"
    assert client.call_tool("boom").startswith("ERROR:")


def test_mcp_tools_adapts_with_risk_and_prefix():
    tools = {t.name: t for t in mcp_tools(MCPClient(_FakeTransport()), server_name="svc")}
    assert set(tools) == {"mcp_svc_echo", "mcp_svc_delete_thing"}
    # "echo" has no read-ish name token; bare readOnlyHint is untrusted → SEND.
    assert tools["mcp_svc_echo"].risk is RiskClass.SEND
    assert tools["mcp_svc_delete_thing"].risk is RiskClass.DESTRUCTIVE
    assert tools["mcp_svc_echo"].run(text="hi") == "called echo"


# --------------------------------------------------------------- governance
def test_external_destructive_tool_is_held():
    tools = mcp_tools(MCPClient(_FakeTransport()), server_name="svc")
    reg = ToolRegistry()
    for t in tools:
        reg.register(t)

    class _CallsDelete:
        def chat_tools(self, messages, tools=None, system=None):
            return {"text": "", "tool_calls": [
                {"id": "c1", "name": "mcp_svc_delete_thing", "args": {}}]}

    broker = GovernanceBroker(GovernancePolicy(allowed_tools=set(reg.names())))
    events = GovernedChatAgent(_CallsDelete(), reg, broker).run(
        [{"role": "user", "content": "delete it"}])
    types = [e.type for e in events]
    assert "approval" in types and "tool_result" not in types


# ------------------------------------------------- end-to-end over OS pipes
def test_stdio_transport_roundtrip_over_pipes():
    c2s_r, c2s_w = os.pipe()  # client -> server
    s2c_r, s2c_w = os.pipe()  # server -> client
    server_reader = os.fdopen(c2s_r, "rb", buffering=0)
    server_writer = os.fdopen(s2c_w, "wb", buffering=0)
    client_reader = os.fdopen(s2c_r, "rb", buffering=0)
    client_writer = os.fdopen(c2s_w, "wb", buffering=0)

    server = threading.Thread(target=serve_stdio,
                              args=(echo_handler, server_reader, server_writer),
                              daemon=True)
    server.start()

    client = MCPClient(StdioTransport(client_reader, client_writer))
    try:
        info = client.initialize()
        assert info["serverInfo"]["name"] == "praxis-echo"
        names = [td["name"] for td in client.list_tools()]
        assert "echo" in names and "delete_record" in names
        assert client.call_tool("echo", {"text": "hello"}) == "echo: hello"
    finally:
        client.close()
        server_writer.close()
        server.join(timeout=3)
