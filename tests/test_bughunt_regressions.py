"""Regression tests for bugs found in the post-Phase-D bug hunt."""
import os

from hybridagent import config as cfg


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path / ".praxis"))


# BUG 1: docker sandbox hardcoded --user 1000:1000 broke non-uid-1000 hosts.
def test_docker_uses_host_uid_not_hardcoded():
    import inspect

    from hybridagent import sandbox
    src = inspect.getsource(sandbox._run_docker)
    # must not hardcode 1000:1000 as the literal user; must derive from getuid
    assert '"1000:1000"' not in src.replace(" ", "") or "getuid" in src
    assert "getuid" in src


def test_docker_user_matches_current_uid(monkeypatch):
    """The --user value handed to docker is the host's real uid:gid."""
    from hybridagent import sandbox
    captured = {}

    class _P:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(cmd, **kw):
        captured["cmd"] = cmd
        return _P()

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
    sandbox._run_docker(["echo", "x"], ".", 10, None, image="img",
                        network="none", mem="512m", pids=128)
    cmd = captured["cmd"]
    uidgid = cmd[cmd.index("--user") + 1]
    assert uidgid == f"{os.getuid()}:{os.getgid()}"


# BUG 2: out-of-range cron fields triggered a ~527k-iteration scan before None.
def test_invalid_cron_fields_fail_fast():
    import time

    from hybridagent.cron import compute_next_run
    for expr in ["60 * * * *", "0 24 * * *", "0 0 32 * *", "0 0 * 13 *",
                 "0 0 * * 9", "*/0 * * * *"]:
        t0 = time.time()
        nr = compute_next_run(expr)
        elapsed = time.time() - t0
        assert nr.ts is None, f"{expr} should be unparseable"
        assert elapsed < 0.05, f"{expr} took {elapsed:.3f}s (should fail fast)"


def test_valid_cron_still_works():
    from datetime import datetime

    from hybridagent.cron import compute_next_run
    nr = compute_next_run("0 9 * * 1")  # Mondays 09:00
    assert nr.ts is not None
    assert datetime.fromtimestamp(nr.ts).hour == 9
    # range boundaries are valid (each field at its max, no dom+dow conflict)
    assert compute_next_run("59 23 28 12 *").ts is not None
    assert compute_next_run("0 0 1 1 *").ts is not None
    assert compute_next_run("0 0 * * 6").ts is not None  # Saturdays


# BUG 3: marketplace silently accepted a version downgrade.
def test_marketplace_rejects_version_downgrade(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    src = tmp_path / "p.py"
    src.write_text("def register(r):\n    pass\n")
    from hybridagent import marketplace as mk
    assert mk.publish(str(src), name="p", version="1.2.0").get("published") == "p"
    res = mk.publish(str(src), name="p", version="1.1.0")
    assert "error" in res and "older" in res["error"]
    # same or newer is fine
    assert mk.publish(str(src), name="p", version="1.2.0").get("published") == "p"
    assert mk.publish(str(src), name="p", version="2.0.0").get("published") == "p"


def test_version_tuple_parsing():
    from hybridagent.marketplace import _version_tuple
    assert _version_tuple("1.2.3") == (1, 2, 3)
    assert _version_tuple("0.1.0") < _version_tuple("0.2.0")
    assert _version_tuple("1.0") < _version_tuple("1.0.1")
    assert _version_tuple("v2.0") == (2, 0)  # tolerant of 'v' prefix
