"""Provider wire-protocol tests against an in-process stub server.

The gated ``test_ollama_integration`` only runs with a real Ollama installed, so
in normal CI it merely skips. These tests close that gap: they stand up a tiny
localhost HTTP server that speaks Ollama's OpenAI-compatible dialect and drive
the *real* ``urllib`` code path in :mod:`hybridagent.providers` — discovery,
chat, embeddings (including out-of-order index reassembly), transient-error
retry, and malformed-response handling — with no third-party dependencies.
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from hybridagent import providers
from hybridagent.providers import CATALOG, chat, discover_ollama_models, embed

_OLLAMA = CATALOG["ollama"]


class _StubHandler(BaseHTTPRequestHandler):
    fail_times = 0          # how many leading /chat calls return 503
    calls = 0               # total POST /chat calls seen

    def log_message(self, *_args):   # silence the default stderr spam
        pass

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.endswith("/api/tags"):
            self._send(200, {"models": [{"name": "llama3.2"},
                                        {"name": "qwen2.5:7b"}]})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path.endswith("/chat/completions"):
            type(self).calls += 1
            if type(self).calls <= type(self).fail_times:
                self._send(503, {"error": "transient upstream"})
                return
            if payload.get("model") == "badshape":
                self._send(200, {"unexpected": "no choices here"})
                return
            self._send(200, {"choices": [{"message": {"content": "pong"}}]})
        elif self.path.endswith("/embeddings"):
            # Return indices out of order to prove the client re-sorts them.
            self._send(200, {"data": [
                {"index": 1, "embedding": [0.3, 0.4]},
                {"index": 0, "embedding": [0.1, 0.2]},
            ]})
        else:
            self._send(404, {"error": "not found"})


@pytest.fixture
def base_url():
    _StubHandler.fail_times = 0
    _StubHandler.calls = 0
    httpd = HTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}/v1"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def test_discover_models_over_the_wire(base_url):
    assert discover_ollama_models(base_url) == ["llama3.2", "qwen2.5:7b"]


def test_discover_models_unreachable_returns_empty():
    # Nothing is listening on this port -> graceful [].
    assert discover_ollama_models("http://127.0.0.1:9/v1", timeout=0.5) == []


def test_chat_roundtrip_over_the_wire(base_url):
    out = chat(provider=_OLLAMA, model="llama3.2", prompt="ping",
               system=None, api_key=None, base_url=base_url, timeout=5.0)
    assert out == "pong"


def test_chat_retries_then_succeeds(base_url, monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)  # no real backoff
    _StubHandler.fail_times = 2          # 503, 503, then 200
    out = chat(provider=_OLLAMA, model="llama3.2", prompt="ping",
               system=None, api_key=None, base_url=base_url, timeout=5.0)
    assert out == "pong"
    assert _StubHandler.calls == 3       # two failures + one success


def test_chat_exhausts_retries_and_raises(base_url, monkeypatch):
    monkeypatch.setattr(providers.time, "sleep", lambda *_: None)
    _StubHandler.fail_times = 99         # always 503 -> retries exhausted
    with pytest.raises(RuntimeError):
        chat(provider=_OLLAMA, model="llama3.2", prompt="ping",
             system=None, api_key=None, base_url=base_url, timeout=5.0)


def test_chat_malformed_response_raises(base_url):
    with pytest.raises(RuntimeError):
        chat(provider=_OLLAMA, model="badshape", prompt="ping",
             system=None, api_key=None, base_url=base_url, timeout=5.0)


def test_embeddings_reassemble_in_index_order(base_url):
    vecs = embed(provider=_OLLAMA, model="nomic-embed-text",
                 texts=["a", "b"], api_key=None, base_url=base_url, timeout=5.0)
    assert vecs == [[0.1, 0.2], [0.3, 0.4]]   # re-sorted by index, not wire order
