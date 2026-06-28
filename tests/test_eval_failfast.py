"""P3 eval fail-fast: per-case timeout guard so the suite never hangs."""
import time

from hybridagent.evals import EvalCase, run_evals


def test_fast_case_passes_under_timeout():
    ok = EvalCase("ok", "test", "fast", lambda: (True, "good"))
    rep = run_evals(cases=[ok], timeout=5.0)
    assert rep.passed and rep.total == 1
    assert rep.results[0].detail == "good"


def test_slow_case_is_failed_not_hung():
    def _hang():
        time.sleep(5)
        return True, "done"

    slow = EvalCase("slow", "test", "hangs", _hang)
    started = time.time()
    rep = run_evals(cases=[slow], timeout=0.3)
    elapsed = time.time() - started
    assert elapsed < 3                       # the suite returned fast, didn't hang
    assert rep.total == 1 and not rep.passed
    assert "timed out" in rep.results[0].detail


def test_timeout_disabled_runs_inline():
    ok = EvalCase("ok", "test", "fast", lambda: (True, "x"))
    rep = run_evals(cases=[ok], timeout=0)   # 0 disables the guard
    assert rep.passed


def test_eval_command_never_prompts_onboarding(monkeypatch):
    # `praxis eval` is a non-interactive offline gate; main() must NOT block on the
    # first-run onboarding input() prompt (the real cause of the "eval hangs" bug).
    from hybridagent import cli

    def _boom(*_a, **_k):
        raise AssertionError("eval must never prompt for onboarding input")

    monkeypatch.setattr("builtins.input", _boom)
    cli._maybe_first_run_onboard("eval")     # exempt -> returns without prompting
