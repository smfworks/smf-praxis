"""Final cheap coverage: cron schedule branches, vault edge cases, plugin errors."""
from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


def test_cron_schedule_forms():
    from hybridagent.cron import compute_next_run, normalize_mode
    # keyword, daily-at, interval, every-prefix all resolve
    assert compute_next_run("hourly").ts is not None
    assert compute_next_run("daily").ts is not None
    assert compute_next_run("weekly").ts is not None
    assert compute_next_run("@09:00").ts is not None
    assert compute_next_run("daily@23:30").ts is not None
    assert compute_next_run("every 15m").ts is not None
    assert compute_next_run("90s").ts is not None
    # bad daily time
    assert compute_next_run("@99:99").ts is None
    # mode normalization
    assert normalize_mode("AGENT") == "agent"
    assert normalize_mode("bogus") == "do"
    assert normalize_mode("") == "do"


def test_cron_interval_too_small():
    from hybridagent.cron import compute_next_run
    assert compute_next_run("0s").ts is None


def test_vault_get_missing_and_delete(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.vault import CredentialVault
    v = CredentialVault()
    assert v.get("nope") is None
    assert v.delete("nope") is False
    assert v.bundles_for_tool("any") == []
    v.put("b", {"K": "v"})
    assert v.get("b") is not None
    assert v.delete("b") is True


def test_vault_inject_unknown_bundle_noop(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent.vault import CredentialVault
    v = CredentialVault()
    # injecting an unknown bundle is a no-op, not an error
    with v.inject("does-not-exist"):
        pass


def test_plugins_load_disabled_skipped(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import plugins
    pdir = plugins.plugins_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "p.py").write_text("def register(r):\n    pass\n")
    from hybridagent.tools import ToolRegistry
    # not enabled -> discovered but not loaded
    infos = plugins.load_plugins(ToolRegistry(), enabled_only=True)
    p = next(i for i in infos if i.name == "p")
    assert not p.loaded and not p.enabled


def test_plugins_no_register_fn(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import plugins
    pdir = plugins.plugins_dir()
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "noreg.py").write_text("x = 1\n")
    plugins.set_enabled("noreg", True)
    from hybridagent.tools import ToolRegistry
    infos = plugins.load_plugins(ToolRegistry())
    p = next(i for i in infos if i.name == "noreg")
    assert not p.loaded
    assert "register" in p.error


def test_marketplace_uninstall_missing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from hybridagent import marketplace as mk
    res = mk.uninstall("never-installed")
    assert res["uninstalled"] == "never-installed"
    assert res["removed"] is False
