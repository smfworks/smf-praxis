"""P4 cost hardening: streaming token usage capture + interactive-chat billing."""
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store
from hybridagent.providers import CATALOG, chat_messages_stream


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _StreamStub(BaseHTTPRequestHandler):
    # HTTP/1.0 so the body is close-delimited (the client reads to EOF).
    protocol_version = "HTTP/1.0"

    def log_message(self, *_a):
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.end_headers()
        for line in (
            'data: {"choices":[{"delta":{"content":"hel"}}]}',
            'data: {"choices":[{"delta":{"content":"lo"}}]}',
            'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":3}}',
            "data: [DONE]",
        ):
            self.wfile.write((line + "\n\n").encode())


def test_openai_stream_captures_usage():
    port = _free_port()
    srv = HTTPServer(("127.0.0.1", port), _StreamStub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        usage: dict = {}
        pieces = list(chat_messages_stream(
            provider=CATALOG["ollama"], model="m",
            messages=[{"role": "user", "content": "hi"}],
            base_url=f"http://127.0.0.1:{port}", usage_sink=usage))
        assert "".join(pieces) == "hello"
        assert usage == {"prompt_tokens": 11, "completion_tokens": 3}
    finally:
        srv.shutdown()


def test_add_spend_count_run_false_does_not_bump_runs(tmp_path):
    s = Store(tmp_path / "b.db")
    s.set_budget_limit(10.0)
    s.add_spend(1.0)                              # a run -> runs++
    assert s.get_budget()["runs"] == 1
    s.add_spend(2.0, count_run=False)            # chat-style accrual -> no runs++
    b = s.get_budget()
    assert abs(b["spent_usd"] - 3.0) < 1e-9 and b["runs"] == 1
    s.close()


def test_daemon_chat_mock_is_free(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    out = d.chat([{"role": "user", "content": "hi"}])
    assert "text" in out
    assert d.budget_status()["spent_usd"] == 0.0   # mock chat accrues nothing
