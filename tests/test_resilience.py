import io
import urllib.error

import pytest

from hybridagent import providers


class _FakeResp:
    def __init__(self, data: bytes):
        self._d = data

    def read(self):
        return self._d

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_post_retries_on_urlerror_then_succeeds(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] < 3:
            raise urllib.error.URLError("temporarily down")
        return _FakeResp(b'{"ok": true}')

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    out = providers._post("http://x", {}, {"a": 1}, timeout=1,
                          retries=2, backoff=0.01)
    assert out == {"ok": True}
    assert calls["n"] == 3


def test_post_retries_on_http_429(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(
                "http://x", 429, "rate", {}, io.BytesIO(b"slow down"))
        return _FakeResp(b'{"ok": 1}')

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    out = providers._post("http://x", {}, {}, timeout=1, retries=2, backoff=0.01)
    assert out == {"ok": 1}
    assert calls["n"] == 2


def test_post_raises_after_exhausting_retries(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("down")

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        providers._post("http://x", {}, {}, timeout=1, retries=1, backoff=0.01)


def test_non_retryable_http_400_raises_immediately(monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        raise urllib.error.HTTPError(
            "http://x", 400, "bad", {}, io.BytesIO(b"bad request"))

    monkeypatch.setattr(providers.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(providers.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError):
        providers._post("http://x", {}, {}, timeout=1, retries=3, backoff=0.01)
    assert calls["n"] == 1                       # 400 is not retried


def test_openai_payload_carries_decoding_defaults(monkeypatch):
    captured = {}

    def fake_post(url, headers, payload, timeout, **k):
        captured.update(payload)
        return {"choices": [{"message": {"content": "hi"}}]}

    monkeypatch.setattr(providers, "_post", fake_post)
    out = providers.chat(providers.CATALOG["openai"], "gpt-4o-mini",
                         "hello", None, "key")
    assert out == "hi"
    assert captured["temperature"] == 0.0
    assert captured["max_tokens"] == 1024
