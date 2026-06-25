"""Gated real-Ollama integration test.

Skipped by default. To exercise a *real* local Ollama backend end-to-end::

    # one-time: install Ollama and pull a small model
    ollama pull llama3.2            # or any chat model
    $env:PRAXIS_OLLAMA_TEST = "1"   # PowerShell  (export on bash)
    pytest tests/test_ollama_integration.py -v

Optional overrides:
    PRAXIS_OLLAMA_URL          base URL (default http://127.0.0.1:11434/v1)
    PRAXIS_OLLAMA_MODEL        force a specific chat model id
    PRAXIS_OLLAMA_EMBED_MODEL  enable the embeddings round-trip (e.g. nomic-embed-text)

Every test degrades to a clean ``skip`` (never a hard failure) when the gate is
off, the server is unreachable, or no model is available — so enabling the gate
on a machine without Ollama can't break a run.
"""
from __future__ import annotations

import os
import urllib.request

import pytest

from hybridagent import config as cfg
from hybridagent import onboard
from hybridagent.llm import LLMClient
from hybridagent.providers import CATALOG, chat, discover_ollama_models, embed
from hybridagent.router import NORMAL, ModelRouter

_GATE = os.environ.get("PRAXIS_OLLAMA_TEST") == "1"

pytestmark = pytest.mark.skipif(
    not _GATE,
    reason="real-Ollama integration test; set PRAXIS_OLLAMA_TEST=1 with a local "
    "Ollama running to enable",
)

_OLLAMA = CATALOG["ollama"]
_BASE = os.environ.get("PRAXIS_OLLAMA_URL", _OLLAMA.base_url)

# Small, commonly-pulled chat models preferred when several are installed.
_PREFERRED = ("llama3.2", "llama3.1", "qwen2.5", "phi3.5", "phi3", "mistral", "gemma2")


def _root() -> str:
    root = _BASE.rstrip("/")
    return root[:-3] if root.endswith("/v1") else root


def _reachable() -> bool:
    try:
        with urllib.request.urlopen(f"{_root()}/api/tags", timeout=3) as resp:
            return resp.status == 200
    except Exception:
        return False


def _pick_model() -> str:
    override = os.environ.get("PRAXIS_OLLAMA_MODEL")
    if override:
        return override
    models = discover_ollama_models(_BASE)
    if not models:
        pytest.skip(
            f"no Ollama models at {_BASE}; run `ollama pull llama3.2` or set "
            "PRAXIS_OLLAMA_MODEL"
        )
    for pref in _PREFERRED:
        for m in models:
            if m.split(":", 1)[0] == pref or m.startswith(pref):
                return m
    return models[0]


@pytest.fixture(scope="module", autouse=True)
def _require_ollama():
    """Skip the whole module if the gate is on but the server isn't up."""
    if not _reachable():
        pytest.skip(f"Ollama not reachable at {_BASE}")


@pytest.fixture(scope="module")
def model() -> str:
    return _pick_model()


def test_discover_models_returns_string_list():
    models = discover_ollama_models(_BASE)
    assert isinstance(models, list)
    assert all(isinstance(m, str) and m for m in models)


def test_router_marks_ollama_ref_local(model):
    assert ModelRouter().is_local_ref(f"ollama/{model}") is True


def test_real_chat_roundtrip(model):
    out = chat(
        provider=_OLLAMA,
        model=model,
        prompt="Reply with exactly one word: pong",
        system="You are a terse integration-test fixture. Answer in one word.",
        api_key=None,
        base_url=_BASE,
        timeout=120.0,
        max_tokens=16,
    )
    assert isinstance(out, str)
    assert out.strip(), "model returned an empty completion"


def test_llmclient_real_mode_completes(tmp_path, monkeypatch, model):
    # Point config at a temp home and onboard the discovered model, then drive
    # the full LLMClient -> router -> providers path against the live server.
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    onboard.run_noninteractive("ollama", model)
    llm = LLMClient(mode="real")
    out = llm.complete("Say hello in a single word.", sensitivity=NORMAL)
    assert isinstance(out, str)
    assert out.strip()


def test_real_embeddings_roundtrip(model):
    emb_model = os.environ.get("PRAXIS_OLLAMA_EMBED_MODEL")
    if not emb_model:
        pytest.skip(
            "set PRAXIS_OLLAMA_EMBED_MODEL (e.g. nomic-embed-text) to test the "
            "/v1/embeddings round-trip"
        )
    vecs = embed(
        provider=_OLLAMA,
        model=emb_model,
        texts=["alpha context", "beta context"],
        api_key=None,
        base_url=_BASE,
        timeout=120.0,
    )
    assert len(vecs) == 2
    assert all(isinstance(v, list) and v for v in vecs)
    assert len(vecs[0]) == len(vecs[1]), "embedding dims must be stable"
