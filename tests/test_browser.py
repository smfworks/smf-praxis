"""Tests for governed browser-use tools (offline fallback + governance)."""

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass
from hybridagent.browser import (
    BrowserSession,
    _extract_title,
    _html_to_text,
    browser_tools,
)
from hybridagent.chat_agent import GovernedChatAgent
from hybridagent.tools import ToolRegistry


def test_html_to_text_and_title():
    html = ("<html><head><title>Hi There</title>"
            "<script>var x = 1;</script></head>"
            "<body><p>Hello <b>world</b></p></body></html>")
    text = _html_to_text(html)
    assert "Hello" in text and "world" in text
    assert "var x" not in text
    assert _extract_title(html) == "Hi There"


class _Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(
            b"<title>Fixture</title><body><h1>Praxis Browser</h1>"
            b"<p>governed browsing works.</p></body>")

    def log_message(self, *a):
        pass


def test_browser_session_navigate_fallback():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Fixture)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        s = BrowserSession(allow_playwright=False)   # force the stdlib fallback
        out = s.navigate(f"http://127.0.0.1:{port}/")
        assert "Fixture" in out
        assert "governed browsing works" in s.read()
        assert "governed browsing" in s.find("governed")
    finally:
        srv.shutdown()


def test_browser_navigate_rejects_bad_scheme():
    assert "unsupported URL scheme" in BrowserSession().navigate("file:///etc/passwd")


def test_browser_tools_risk_classes():
    tools = {t.name: t for t in browser_tools()}
    assert tools["browser_navigate"].risk is RiskClass.READ
    assert tools["browser_read"].risk is RiskClass.READ
    assert tools["browser_find"].risk is RiskClass.READ
    assert tools["browser_click"].risk is RiskClass.SEND
    assert tools["browser_type"].risk is RiskClass.SEND
    for tool in tools.values():
        assert tool.parameters and "type" in tool.parameters


class _ClickLLM:
    def __init__(self):
        self.n = 0

    def chat_tools(self, messages, tools, system=None):
        self.n += 1
        if self.n == 1:
            return {"text": "", "tool_calls": [{
                "id": "c1", "name": "browser_click",
                "args": {"target": "#submit"}}]}
        return {"text": "Held for your approval.", "tool_calls": []}


def test_browser_click_is_held_by_broker():
    reg = ToolRegistry()
    for tool in browser_tools():
        reg.register(tool)
    broker = GovernanceBroker(GovernancePolicy(allowed_tools=set(reg.names())))
    agent = GovernedChatAgent(_ClickLLM(), reg, broker)
    events = list(agent.run([{"role": "user", "content": "click submit"}]))
    types = [e.type for e in events]
    # A click is consequential -> held for approval, never executed.
    assert "approval" in types
    assert "tool_result" not in types
    assert len(broker.pending) == 1
