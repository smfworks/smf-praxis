"""D5a real token-cost accounting: pricing math, usage parsing, accumulator."""
from hybridagent import config as cfg
from hybridagent.llm import LLMClient
from hybridagent.pricing import PRICING, price_usd
from hybridagent.providers import _usage_of


# ------------------------------------------------------------------- pricing
def test_local_and_mock_are_free():
    assert price_usd("mock", 1000, 1000) == 0.0
    assert price_usd("ollama/llama3.1", 5000, 5000) == 0.0


def test_known_cloud_model_priced():
    p, c = PRICING["gpt-4o-mini"]
    # 1K prompt + 1K completion -> exactly (p + c).
    assert abs(price_usd("openai/gpt-4o-mini", 1000, 1000) - (p + c)) < 1e-9
    # Substring match: a dated/suffixed model id still resolves.
    assert price_usd("openai/gpt-4o-mini-2024-07-18", 1000, 0) > 0


def test_unknown_cloud_uses_nonzero_default():
    # Never silently under-count an unrecognised paid model.
    assert price_usd("openai/some-new-frontier-model", 1000, 1000) > 0


def test_longest_key_wins():
    # "gpt-4o-mini" must out-match the shorter "gpt-4o" substring.
    mini = price_usd("openai/gpt-4o-mini", 1000, 1000)
    big = price_usd("openai/gpt-4o", 1000, 1000)
    assert mini < big


# -------------------------------------------------------------- usage parsing
def test_usage_of_normalises_both_wire_shapes():
    oai = _usage_of({"usage": {"prompt_tokens": 12, "completion_tokens": 5}})
    assert oai == {"prompt_tokens": 12, "completion_tokens": 5}
    anth = _usage_of({"usage": {"input_tokens": 7, "output_tokens": 9}})
    assert anth == {"prompt_tokens": 7, "completion_tokens": 9}
    assert _usage_of({}) == {"prompt_tokens": 0, "completion_tokens": 0}


# ------------------------------------------------------------- accumulator
def test_llmclient_accumulates_and_resets():
    c = LLMClient(mode="mock")
    c.reset_usage()
    c._account("openai/gpt-4o-mini", {"prompt_tokens": 1000, "completion_tokens": 1000})
    c._account("openai/gpt-4o-mini", {"prompt_tokens": 0, "completion_tokens": 500})
    snap = c.usage_snapshot()
    assert snap["calls"] == 2
    assert snap["prompt_tokens"] == 1000 and snap["completion_tokens"] == 1500
    assert snap["cost_usd"] > 0
    assert snap["model"] == "openai/gpt-4o-mini"
    c.reset_usage()
    assert c.usage_snapshot()["cost_usd"] == 0.0 and c.usage_snapshot()["calls"] == 0


def test_local_calls_accrue_no_cost():
    c = LLMClient(mode="mock")
    c.reset_usage()
    c._account("ollama/llama3.1", {"prompt_tokens": 9999, "completion_tokens": 9999})
    snap = c.usage_snapshot()
    assert snap["calls"] == 1 and snap["cost_usd"] == 0.0


# -------------------------------------------------------------------- daemon
def test_agent_run_mock_is_free_with_usage(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))
    res = d.agent_run("draft a short note")
    assert "usage" in res and res["usage"]["cost_usd"] == 0.0
    assert d.budget_status()["spent_usd"] == 0.0     # mock spends nothing
