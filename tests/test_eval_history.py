from hybridagent.eval_history import RegressionReport, compare_reports
from hybridagent.persistence import Store


def _rep(pairs):
    return {"cases": [{"id": cid, "passed": p} for cid, p in pairs]}


def test_compare_detects_regression_fix_added_removed():
    base = _rep([("a", True), ("b", True), ("c", False), ("x", True)])
    curr = _rep([("a", True), ("b", False), ("c", True), ("d", True)])
    rr = compare_reports(base, curr)
    assert rr.regressions == ["b"]
    assert rr.fixes == ["c"]
    assert rr.added == ["d"]
    assert rr.removed == ["x"]
    assert rr.ok is False


def test_ok_when_only_fixes_and_additions():
    rr = compare_reports(_rep([("a", True), ("b", False)]),
                         _rep([("a", True), ("b", True), ("c", True)]))
    assert rr.ok is True
    assert rr.fixes == ["b"] and rr.added == ["c"]


def test_identical_reports_have_no_changes():
    rep = _rep([("a", True), ("b", True)])
    rr = compare_reports(rep, rep)
    assert rr.ok and not rr.regressions and not rr.fixes
    assert not rr.added and not rr.removed
    assert "no regressions" in rr.render()


def test_render_flags_regression():
    rr = compare_reports(_rep([("a", True)]), _rep([("a", False)]))
    out = rr.render()
    assert "REGRESSION DETECTED" in out and "a" in out


def test_regression_report_dataclass_defaults():
    rr = RegressionReport()
    assert rr.ok and rr.render() == "OK — no regressions vs baseline."


def test_store_eval_runs_roundtrip(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        assert store.list_eval_runs() == []
        rid = store.save_eval_run('{"cases":[]}', passes=18, total=20)
        assert rid > 0
        store.save_eval_run('{"cases":[]}', passes=20, total=20)
        runs = store.list_eval_runs(limit=10)
        assert len(runs) == 2
        assert runs[0]["passes"] == 20 and runs[1]["passes"] == 18  # newest first
    finally:
        store.close()


def test_store_eval_baseline_roundtrip(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        assert store.load_eval_baseline() is None
        store.save_eval_baseline('{"cases":[{"id":"a","passed":true}]}')
        loaded = store.load_eval_baseline()
        assert loaded["cases"][0]["id"] == "a"
        # Upsert replaces, not duplicates.
        store.save_eval_baseline('{"cases":[{"id":"b","passed":true}]}')
        assert store.load_eval_baseline()["cases"][0]["id"] == "b"
    finally:
        store.close()


def test_baseline_gate_against_live_suite(tmp_path):
    from hybridagent.evals import run_evals
    data = run_evals().to_dict()
    store = Store.open(tmp_path / "praxis.db")
    try:
        import json
        store.save_eval_baseline(json.dumps(data))
        base = store.load_eval_baseline()
        # Comparing the suite to itself => no regressions.
        assert compare_reports(base, data).ok
        # Flip one case to failing => a regression is detected.
        regressed = json.loads(json.dumps(data))
        regressed["cases"][0]["passed"] = False
        rr = compare_reports(base, regressed)
        assert not rr.ok and regressed["cases"][0]["id"] in rr.regressions
    finally:
        store.close()
