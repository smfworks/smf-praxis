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


def test_chat_messages_roundtrip_over_the_wire(base_url):
    from hybridagent.providers import chat_messages
    out = chat_messages(provider=_OLLAMA, model="llama3.2",
                        messages=[{"role": "user", "content": "hi"},
                                  {"role": "assistant", "content": "hello"},
                                  {"role": "user", "content": "ping"}],
                        system="be terse", api_key=None,
                        base_url=base_url, timeout=5.0)
    assert out == "pong"


def test_chat_messages_malformed_response_raises(base_url):
    from hybridagent.providers import chat_messages
    with pytest.raises(RuntimeError):
        chat_messages(provider=_OLLAMA, model="badshape",
                      messages=[{"role": "user", "content": "ping"}],
                      api_key=None, base_url=base_url, timeout=5.0)


# ======================================================================
# Reasoning-model support — content null, reasoning present (Slice 6 prep)
# ======================================================================
def test_chat_falls_back_to_reasoning_when_content_null(base_url):
    """Reasoning models (Qwen3, Kimi thinking, DeepSeek-R1) return content=null
    with the chain-of-thought in `reasoning`. Praxis must fall back so a
    low-token pass still returns text instead of empty/failing."""
    # Drive a request whose stub returns the reasoning-model shape.
    # We can't easily vary the stub per-test without a fixture, so call
    # _chat_openai directly with a monkeypatched _post.
    from hybridagent.providers import _chat_openai

    reasoning_response = {
        "choices": [{"message": {
            "role": "assistant", "content": None,
            "reasoning": "The answer is 4 because 2+2=4.",
        }, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 12, "total_tokens": 22},
    }
    captured = {}
    import hybridagent.providers as p
    real_post = p._post
    def fake_post(url, headers, payload, timeout, retries=2, backoff=0.5):
        captured["payload"] = payload
        return reasoning_response
    monkeypatch_target = p
    try:
        monkeypatch_target._post = fake_post
        out = _chat_openai("http://stub/v1", "qwen3", "prompt", None,
                           None, 5.0, 0.0, 1024)
    finally:
        monkeypatch_target._post = real_post
    assert out == "The answer is 4 because 2+2=4.", f"got {out!r}"


def test_chat_uses_content_when_present(base_url):
    """Normal models return content; the reasoning fallback must not interfere."""
    import hybridagent.providers as p
    from hybridagent.providers import _chat_openai
    normal_response = {
        "choices": [{"message": {
            "role": "assistant", "content": "pong", "reasoning": "thinking...",
        }}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
    }
    real_post = p._post
    p._post = lambda *a, **k: normal_response
    try:
        out = _chat_openai("http://stub/v1", "llama3.2", "ping", None,
                           None, 5.0, 0.0, 1024)
    finally:
        p._post = real_post
    assert out == "pong", f"got {out!r}"


def test_chat_empty_when_both_content_and_reasoning_absent(base_url):
    """No content and no reasoning -> empty string, not a crash."""
    import hybridagent.providers as p
    from hybridagent.providers import _chat_openai
    empty_response = {
        "choices": [{"message": {"role": "assistant", "content": None}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 0, "total_tokens": 5},
    }
    real_post = p._post
    p._post = lambda *a, **k: empty_response
    try:
        out = _chat_openai("http://stub/v1", "model", "prompt", None,
                           None, 5.0, 0.0, 1024)
    finally:
        p._post = real_post
    assert out == "", f"got {out!r}"


def test_extract_text_reasoning_content_variant():
    """Some servers use `reasoning_content` instead of `reasoning`."""
    from hybridagent.providers import _extract_text
    assert _extract_text({"content": None, "reasoning_content": "via variant"}) == "via variant"
    assert _extract_text({"content": None, "reasoning": "via primary"}) == "via primary"
    # content wins over reasoning when both present
    assert _extract_text({"content": "final", "reasoning": "thinking"}) == "final"
    # empty content falls back to reasoning
    assert _extract_text({"content": "", "reasoning": "thinking"}) == "thinking"
    # truly empty -> empty string
    assert _extract_text({"content": None}) == ""
    assert _extract_text(None) == ""
    assert _extract_text({}) == ""
