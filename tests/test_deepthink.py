from hybridagent.deepthink import DeepThink, DeepThinkResult

_HARD = "Analyze the trade-offs and design the optimal architecture for this"
_SIMPLE = "hi there"


def test_should_engage_on_hard_only():
    dt = DeepThink(lambda t, d: "x")
    assert dt.should_engage(_HARD) is True
    assert dt.should_engage(_SIMPLE) is False
    assert dt.should_engage(_SIMPLE, force=True) is True


def test_single_pass_when_not_hard():
    calls = []

    def solver(task, directive):
        calls.append(directive)
        return "quick answer"

    res = DeepThink(solver).solve(_SIMPLE)
    assert isinstance(res, DeepThinkResult)
    assert res.engaged is False and res.rounds == 0
    assert res.answer == "quick answer" and len(calls) == 1


def test_consensus_in_round_one_skips_extra_rounds():
    def solver(task, directive):
        return "The answer is 42."  # all stances agree immediately

    res = DeepThink(solver, rounds=3).solve(_HARD)
    assert res.engaged and res.rounds == 1
    assert res.votes == 3 and "42" in res.answer


def test_second_round_resolves_no_consensus():
    round1 = iter(["alpha wins", "beta wins", "gamma wins"])

    def solver(task, directive):
        if "Other attempts" in directive:
            return "On reflection, alpha wins."
        return next(round1)

    res = DeepThink(solver, rounds=2).solve(_HARD)
    assert res.engaged and res.rounds == 2
    assert res.votes == 3 and "alpha" in res.answer.lower()


def test_round_budget_is_bounded():
    # Solvers never converge (each answer shares no tokens); deep-think must stop
    # at the round budget.
    seq = iter(["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"])

    def solver(task, directive):
        return next(seq)

    res = DeepThink(solver, rounds=2).solve(_HARD)
    assert res.rounds == 2  # bounded, even with perpetual disagreement


def test_force_engages_on_simple_goal():
    def solver(task, directive):
        return "deliberated answer"

    res = DeepThink(solver).solve(_SIMPLE, force=True)
    assert res.engaged and res.rounds >= 1


def test_verifier_flags_evasive_final():
    def solver(task, directive):
        return ""  # empty -> non-evasive verifier failure

    res = DeepThink(solver).solve(_HARD)
    assert res.engaged and res.approved is False
