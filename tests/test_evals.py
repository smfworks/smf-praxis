"""Meta-tests for the eval flywheel: the built-in suite passes, categories are
present, the category filter works, and a deliberately failing case is caught."""

from hybridagent.evals import BUILTIN_EVALS, EvalCase, run_evals
from hybridagent.vertical_evals import vertical_eval_cases


def test_builtin_evals_all_pass():
    report = run_evals()
    assert report.total == len(BUILTIN_EVALS) + len(vertical_eval_cases())
    assert report.passed, report.render()


def test_eval_categories_present():
    cats = {c.category for c in BUILTIN_EVALS}
    assert {"tool_use", "approval", "safety", "schema"} <= cats


def test_category_filter():
    report = run_evals(category="safety")
    assert report.total >= 1
    assert all(r.category == "safety" for r in report.results)


def test_failing_case_is_detected():
    bad = EvalCase("synthetic.fail", "meta", "always fails",
                   lambda: (False, "nope"))
    report = run_evals(cases=[bad])
    assert not report.passed
    assert report.passes == 0
    assert "nope" in report.render()


def test_crashing_case_counts_as_failure():
    def _boom():
        raise RuntimeError("kaboom")

    bad = EvalCase("synthetic.boom", "meta", "raises", _boom)
    report = run_evals(cases=[bad])
    assert not report.passed
    assert "kaboom" in report.render()
