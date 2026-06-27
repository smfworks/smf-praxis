from hybridagent.orchestrator import Orchestrator, PredictiveRouter
from hybridagent.persistence import Store
from hybridagent.router_model import (
    RouterModel,
    samples_from_runs,
    train_from_runs,
)

# Outcome history with role-distinctive vocabulary (no overlap with the
# heuristic's hard-coded keywords, so a correct route is genuinely *learned*).
_SAMPLES = [
    ("review the vendor contract for regulatory exposure", "compliance"),
    ("assess regulatory exposure for this clinical workflow", "compliance"),
    ("examine access controls for governance gaps", "compliance"),
    ("write a warm note to the customer thread", "drafter"),
    ("compose a response to the partner about timing", "drafter"),
    ("put together a short message confirming the change", "drafter"),
    ("estimate next quarter pipeline movement", "predictor"),
    ("project the churn for these renewing accounts", "predictor"),
    ("gather background on the interoperability framework", "researcher"),
    ("collect notes and sources about the agenda topic", "researcher"),
]


def test_train_and_predict_picks_right_role():
    model = RouterModel.train(_SAMPLES)
    role, conf = model.predict("evaluate regulatory exposure on this vendor")
    assert role == "compliance"
    assert 0.0 < conf <= 1.0


def test_json_roundtrip_preserves_predictions():
    model = RouterModel.train(_SAMPLES)
    clone = RouterModel.from_json(model.to_json())
    goal = "compose a response for the customer about timing"
    assert clone.predict(goal) == model.predict(goal)
    assert clone.classes == model.classes and clone.n_samples == model.n_samples


def test_confident_gates_on_threshold():
    # Two classes with disjoint vocab and equal priors -> a goal that mixes one
    # token from each is a near-tie and must not clear a 0.6 threshold.
    model = RouterModel.train([
        ("alpha alpha", "researcher"), ("alpha alpha", "researcher"),
        ("beta beta", "drafter"), ("beta beta", "drafter"),
    ])
    assert model.confident("alpha alpha", threshold=0.6) == "researcher"
    assert model.confident("alpha beta", threshold=0.6) is None


def test_empty_model_predicts_nothing():
    model = RouterModel()
    assert model.predict("anything") == (None, 0.0)
    assert model.confident("anything") is None


def test_router_uses_model_when_confident_else_heuristic():
    r = PredictiveRouter(model=RouterModel.train(_SAMPLES), threshold=0.55)
    # No heuristic keyword -> heuristic would say 'researcher'; model says compliance.
    assert r.route("evaluate regulatory exposure on this vendor engagement") == \
        "compliance"
    # Heuristic-only router on the same goal falls through to researcher.
    assert PredictiveRouter().route(
        "evaluate regulatory exposure on this vendor engagement") == "researcher"


def test_injection_flag_pins_researcher_despite_model():
    r = PredictiveRouter(model=RouterModel.train(_SAMPLES), threshold=0.55)
    # Without the flag the model would route this to drafter.
    assert r.route("compose a response to the customer") == "drafter"
    # With the flag it is pinned to the most-restrictive role.
    assert r.route("compose a response to the customer",
                   injection_flagged=True) == "researcher"


def test_unknown_learned_role_is_ignored():
    # A model that confidently emits a role outside the known set must not be
    # honoured; the router falls back to the heuristic instead.
    model = RouterModel.train([
        ("abracadabra incantation hex", "wizard"),
        ("abracadabra incantation hex", "wizard"),
        ("gather notes and sources", "researcher"),
        ("gather notes and sources", "researcher"),
    ])
    assert "wizard" not in PredictiveRouter.KNOWN_ROLES
    r = PredictiveRouter(model=model, threshold=0.55)
    assert r.route("abracadabra incantation hex") == "researcher"


def test_samples_from_runs_drops_failures():
    runs = [
        {"goal": "g1", "role": "drafter", "status": "completed"},
        {"goal": "g2", "role": "compliance", "status": "waiting_approval"},
        {"goal": "g3", "role": "drafter", "status": "failed"},
        {"goal": "", "role": "drafter", "status": "completed"},
    ]
    pairs = samples_from_runs(runs)
    assert ("g1", "drafter") in pairs and ("g2", "compliance") in pairs
    assert ("g3", "drafter") not in pairs  # failed run is not positive evidence
    assert all(goal for goal, _ in pairs)  # blank goal dropped


def test_train_from_runs_returns_none_without_enough_signal():
    one_class = [{"goal": f"g{i}", "role": "drafter", "status": "completed"}
                 for i in range(12)]
    assert train_from_runs(one_class) is None  # only one role
    too_few = [{"goal": "a", "role": "drafter", "status": "completed"},
               {"goal": "b", "role": "compliance", "status": "completed"}]
    assert train_from_runs(too_few, min_samples=8) is None


def test_store_persists_and_reloads_model(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        assert store.load_router_model() is None
        model = RouterModel.train(_SAMPLES)
        store.save_router_model(model.to_json(), n_samples=model.n_samples)
        rec = store.load_router_model()
        assert rec and rec["n_samples"] == model.n_samples
        loaded = PredictiveRouter.from_store(store, threshold=0.55)
        assert loaded.model is not None
        assert loaded.route("evaluate regulatory exposure on this vendor") == \
            "compliance"
    finally:
        store.close()


def test_orchestrator_train_router_end_to_end(tmp_path):
    store = Store.open(tmp_path / "praxis.db")
    try:
        for i, (goal, role) in enumerate(_SAMPLES):
            store.add_subagent_run(f"run-{i}", f"agent-{role}", role, goal,
                                   status="completed")
        orch = Orchestrator(store)
        assert orch.router.model is None  # nothing trained yet -> heuristic
        model = orch.train_router(min_samples=8)
        assert model is not None
        assert orch.router.model is not None  # swapped in
        assert orch.router.route(
            "evaluate regulatory exposure on this vendor") == "compliance"
        # Persisted, so a fresh orchestrator auto-loads it.
        assert Orchestrator(store).router.model is not None
    finally:
        store.close()
