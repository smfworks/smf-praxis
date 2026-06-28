"""D4.1 kill-switch real-disable path: persists across restart + halts new runs."""
from hybridagent import config as cfg
from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass, Verdict
from hybridagent.llm import LLMClient
from hybridagent.persistence import Store


# ----------------------------------------------------------------------- store
def test_killswitch_store_roundtrip(tmp_path):
    db = tmp_path / "ks.db"
    s = Store(db)
    assert s.get_killswitch() is False        # default released
    assert s.set_killswitch(True) is True
    s.close()

    # A fresh Store on the same db sees the engaged brake -- it survived "restart".
    s2 = Store(db)
    assert s2.get_killswitch() is True
    s2.set_killswitch(False)
    assert s2.get_killswitch() is False
    s2.close()


# ---------------------------------------------------------------------- broker
def test_broker_killswitch_persists(tmp_path):
    db = tmp_path / "ks.db"
    s = Store(db)
    pol = GovernancePolicy(allowed_tools={"send_email"})
    b = GovernanceBroker(pol, store=s)
    assert b.kill.tripped is False
    b.kill.trip()

    # A fresh broker hydrated from the same store starts already tripped.
    b2 = GovernanceBroker(pol, store=s)
    assert b2.kill.tripped is True
    b2.kill.reset()

    b3 = GovernanceBroker(pol, store=s)
    assert b3.kill.tripped is False
    s.close()


def test_broker_without_store_still_works():
    # No store -> in-memory only, no persistence calls, no crash.
    b = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    assert b.kill.tripped is False
    b.kill.trip()
    assert b.kill.tripped is True


def test_tripped_broker_denies_consequential_tool():
    # Defense in depth: even mid-flight, a tripped broker denies a consequential
    # tool at the authorization gate with the kill_switch_denied policy rule.
    b = GovernanceBroker(GovernancePolicy(allowed_tools={"send_email"}))
    b.kill.trip()
    dec = b.authorize("agent", "send_email", RiskClass.SEND, {"to": "x@example.com"})
    assert dec.verdict == Verdict.DENY
    assert dec.policy_rule == "kill_switch_denied"


# ---------------------------------------------------------------------- daemon
def test_agent_run_blocked_when_engaged(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))
    from hybridagent.daemon import Daemon
    d = Daemon(llm=LLMClient(mode="mock"))

    assert d.killswitch_set(True)["engaged"] is True
    res = d.agent_run("send the customer update")
    assert res["status"] == "blocked" and res.get("blocked") is True
    assert not res["run_id"]                   # never started a run

    # Releasing the brake lets runs proceed again.
    assert d.killswitch_set(False)["engaged"] is False
    ok = d.agent_run("draft a short summary")
    assert ok["status"] != "blocked"
