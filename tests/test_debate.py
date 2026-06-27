from hybridagent.debate import Candidate, DebatePanel, DebateResult, _jaccard, _tokens


def _panel(answer_list, **kw):
    it = iter(answer_list)

    def solver(task, stance):
        return next(it)

    return DebatePanel(solver, **kw)


def test_jaccard_basics():
    assert _jaccard(_tokens("a b c"), _tokens("a b c")) == 1.0
    assert _jaccard(_tokens("a b"), _tokens("c d")) == 0.0
    assert _jaccard(set(), set()) == 1.0


def test_majority_consensus_wins():
    res = _panel([
        "The capital of France is Paris.",
        "Paris is the capital of France.",
        "It might be Lyon, not sure.",
    ]).debate("capital of France?")
    assert isinstance(res, DebateResult)
    assert "paris" in res.answer.lower()
    assert res.votes == 2


def test_unanimous_agreement():
    res = _panel(["Yes, it is 42.", "It is 42, yes.", "42 — yes it is."]).debate("?")
    assert res.votes == 3


def test_all_dissent_picks_first():
    res = _panel(["alpha one", "beta two", "gamma three"]).debate("?")
    assert res.votes == 1
    assert res.answer == "alpha one"


def test_verifier_filters_empty_candidate():
    res = _panel(["", "Paris.", "Paris."]).debate("capital?")
    assert res.answer == "Paris." and res.votes == 2
    approved = [c for c in res.candidates if c.approved]
    assert len(approved) == 2  # the empty candidate failed verification


def test_judge_breaks_tie_when_no_majority():
    res = _panel(
        ["alpha one", "beta two", "gamma three"],
        judge=lambda task, answers: 2,
    ).debate("?")
    assert res.answer == "gamma three"  # judge override on all-singleton clusters


def test_judge_ignored_when_majority_exists():
    res = _panel(
        ["Paris is the capital.", "The capital is Paris.", "Lyon perhaps."],
        judge=lambda task, answers: 2,  # would pick Lyon
    ).debate("?")
    assert "lyon" not in res.answer.lower()  # consensus wins, judge not consulted


def test_solver_exception_does_not_crash():
    def solver(task, stance):
        raise RuntimeError("boom")

    res = DebatePanel(solver).debate("?")
    assert res.answer == "" and len(res.candidates) == 3


def test_custom_stances_control_candidate_count():
    res = _panel(["a", "b"], stances=("s1", "s2")).debate("?")
    assert len(res.candidates) == 2
    assert [c.stance for c in res.candidates] == ["s1", "s2"]


def test_candidate_dataclass_defaults():
    c = Candidate(answer="x", stance="s")
    assert c.approved is True
