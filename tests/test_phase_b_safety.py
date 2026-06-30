"""Phase B safety: agent identity (G8), policy hook (G8), sandbox backend (G6)."""
import os

from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


# ------------------------------------------------------------------- identity
def test_identity_create_and_verify(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.identity import AgentIdentity
    i = AgentIdentity.load_or_create("praxis")
    att = i.attest("send_message", {"target": "ntfy:x", "text": "hi"})
    assert i.verify(att)


def test_identity_detects_tampering(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.identity import AgentIdentity
    i = AgentIdentity.load_or_create("praxis")
    att = i.attest("read_file", {"name": "notes.txt"})
    att.action = "delete_everything"   # tamper
    assert not i.verify(att)


def test_identity_stable_across_reload(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.identity import AgentIdentity
    a = AgentIdentity.load_or_create("praxis")
    b = AgentIdentity.load_or_create("praxis")
    assert a.public_id == b.public_id


def test_identity_file_is_private(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.identity import AgentIdentity, _identity_path
    AgentIdentity.load_or_create("praxis")
    if os.name == "nt":
        # Windows restricts via icacls (ACLs), not POSIX mode bits; just ensure
        # the file exists and secure_file ran without error.
        assert _identity_path().exists()
        return
    mode = os.stat(_identity_path()).st_mode & 0o777
    assert mode == 0o600


# ---------------------------------------------------------------- policy hook
def test_policy_hook_deny_overrides(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass

    def hook(ctx):
        return "deny" if "production" in str(ctx["args"]).lower() else None

    b = GovernanceBroker(policy=GovernancePolicy(
        allowed_tools={"read_file"}, policy_hook=hook))
    assert b.authorize("a", "read_file", RiskClass.READ,
                       {"name": "notes"}).verdict.value == "allow"
    assert b.authorize("a", "read_file", RiskClass.READ,
                       {"name": "production.db"}).verdict.value == "deny"


def test_policy_hook_fails_safe(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass

    def broken(ctx):
        raise RuntimeError("boom")

    b = GovernanceBroker(policy=GovernancePolicy(
        allowed_tools={"read_file"}, policy_hook=broken))
    # a raising hook must deny, never open access
    assert b.authorize("a", "read_file", RiskClass.READ,
                       {"name": "x"}).verdict.value == "deny"


def test_policy_hook_allow_shortcircuits(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.broker import GovernanceBroker, GovernancePolicy, RiskClass

    def hook(ctx):
        return "allow"

    b = GovernanceBroker(policy=GovernancePolicy(
        allowed_tools={"send_message"}, policy_hook=hook))
    # SEND would normally be held; explicit allow short-circuits it
    assert b.authorize("a", "send_message", RiskClass.SEND,
                       {"target": "x", "text": "y"}).verdict.value == "allow"


# ------------------------------------------------------------------- sandbox
def test_sandbox_local_exec(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.sandbox import run
    r = run(["echo", "hello"], workdir=str(tmp_path), backend="local")
    assert r.ok and "hello" in r.stdout


def test_sandbox_select_backend_defaults_local(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.sandbox import select_backend
    # no config -> local
    assert select_backend() == "local"


def test_sandbox_status_shape(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.sandbox import backend_status
    st = backend_status()
    assert st["configured"] == "local"
    assert "docker_available" in st


def test_sandbox_string_command(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.sandbox import run
    r = run("echo via-shell", workdir=str(tmp_path), backend="local")
    assert r.ok and "via-shell" in r.stdout


def test_readiness_includes_sandbox(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.readiness import run_checks
    keys = {c.key for c in run_checks()}
    assert "sandbox" in keys
