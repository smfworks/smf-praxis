"""PRA-005 regression: token auto-minting must warn and secure the config file.

The daemon auto-generates and persists a shared token when binding beyond
loopback with no token configured. That is a surprising side effect — an
operator may not realize a credential was silently written to praxis.json.
ensure_token() must:

1. restrict the config file to 0600 (POSIX) / ACL (Windows) after minting,
2. emit a prominent stderr WARNING naming the config path, and
3. still return a usable, idempotent token (second call returns the same one).
"""

import os
import stat

from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_ensure_token_warns_on_stderr(tmp_path, monkeypatch, capsys):
    """Minting a token prints a prominent security warning to stderr."""
    _isolate(tmp_path, monkeypatch)
    from hybridagent import auth_gate
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    token = auth_gate.ensure_token()
    assert len(token) >= 16, "a token was minted"
    err = capsys.readouterr().err
    assert "PRAXIS SECURITY" in err, "warning banner present on stderr"
    assert "auto-generated auth token" in err
    assert "praxis.json" in err
    assert "PRAXIS_AUTH_TOKEN" in err, "tells operator how to set their own"


def test_ensure_token_secures_config_file(tmp_path, monkeypatch):
    """After minting, the config file is restricted to the owner (0600 on POSIX)."""
    _isolate(tmp_path, monkeypatch)
    from hybridagent import auth_gate
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    auth_gate.ensure_token()
    path = cfg.config_path()
    assert path.exists(), "config file was written"
    if os.name == "posix":
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600, f"config file should be 0600, got {oct(mode)}"


def test_ensure_token_is_idempotent_and_does_not_rewarn(tmp_path, monkeypatch, capsys):
    """A second call returns the existing token and does NOT re-warn."""
    _isolate(tmp_path, monkeypatch)
    from hybridagent import auth_gate
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    first = auth_gate.ensure_token()
    capsys.readouterr()  # drain the first warning
    second = auth_gate.ensure_token()
    assert first == second, "second call returns the same token"
    err = capsys.readouterr().err
    assert "PRAXIS SECURITY" not in err, "no re-warning when token already set"


def test_ensure_token_respects_env_override(tmp_path, monkeypatch, capsys):
    """A pre-set PRAXIS_AUTH_TOKEN is used as-is — no mint, no warning."""
    _isolate(tmp_path, monkeypatch)
    from hybridagent import auth_gate
    monkeypatch.setenv("PRAXIS_AUTH_TOKEN", "my-explicit-token-abc123")
    token = auth_gate.ensure_token()
    assert token == "my-explicit-token-abc123"
    err = capsys.readouterr().err
    assert "PRAXIS SECURITY" not in err, "no warning when env token is set"


def test_ensure_token_sets_minted_flag(tmp_path, monkeypatch):
    """The minted flag is recorded so the dashboard can surface auto-mint state."""
    _isolate(tmp_path, monkeypatch)
    from hybridagent import auth_gate
    monkeypatch.delenv("PRAXIS_AUTH_TOKEN", raising=False)
    auth_gate.ensure_token()
    conf = cfg.load_config()
    assert conf["agents"]["auth"]["minted"] is True