"""Tests for `praxis update` and praxis.json schema migrations."""

import argparse
import json
import urllib.request

from hybridagent import cli
from hybridagent import config as cfg


def test_migrate_config_stamps_and_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path))
    cfg.save_config({"agents": {}})                 # a pre-versioned config
    assert cfg.migrate_config() == cfg.CONFIG_VERSION
    data = cfg.load_config()
    assert data["configVersion"] == cfg.CONFIG_VERSION
    assert data["agents"] == {}                     # existing content preserved
    assert cfg.migrate_config() is None             # already current -> no-op


def test_migrate_config_no_file_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path))
    assert cfg.migrate_config() is None


def test_update_check_editable_guard(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_editable_install", lambda: True)
    rc = cli.cmd_update(argparse.Namespace(check=True))
    out = capsys.readouterr().out.lower()
    assert rc == 1
    assert "editable" in out or "git pull" in out


def test_update_check_reports_newer(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_latest_github_version", lambda: "999.0.0")
    rc = cli.cmd_update(argparse.Namespace(check=True))
    out = capsys.readouterr().out
    assert rc == 0 and "999.0.0" in out


def test_update_check_up_to_date(monkeypatch, capsys):
    from hybridagent import __version__
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_latest_github_version", lambda: __version__)
    rc = cli.cmd_update(argparse.Namespace(check=True))
    out = capsys.readouterr().out.lower()
    assert rc == 0 and "up to date" in out


def test_latest_github_version_requires_strict_release_tag(monkeypatch):
    class Response:
        def __init__(self, tag: object) -> None:
            self._tag = tag

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self) -> bytes:
            return json.dumps({"tag_name": self._tag}).encode()

    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response("v0.26.16"))
    assert cli._latest_github_version() == "0.26.16"
    monkeypatch.setattr(urllib.request, "urlopen", lambda *_args, **_kwargs: Response("latest"))
    assert cli._latest_github_version() is None


def test_update_installs_exact_github_release_wheel(monkeypatch, capsys):
    import subprocess

    calls: list[list[str]] = []
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_latest_github_version", lambda: "99.99.99")
    monkeypatch.setattr(subprocess, "call", lambda command: calls.append(command) or 0)
    monkeypatch.setattr(cfg, "migrate_config", lambda: None)

    rc = cli.cmd_update(argparse.Namespace(check=False))

    assert rc == 0
    assert len(calls) == 1
    url = calls[0][-1]
    assert url == (
        "https://github.com/smfworks/smf-praxis/releases/download/"
        "v99.99.99/praxis_agent-99.99.99-py3-none-any.whl"
    )
    assert "Updated" in capsys.readouterr().out


def test_update_never_downgrades_newer_install(monkeypatch, capsys):
    import subprocess

    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_latest_github_version", lambda: "0.25.20")
    monkeypatch.setattr(
        subprocess,
        "call",
        lambda _command: (_ for _ in ()).throw(AssertionError("must not downgrade")),
    )
    monkeypatch.setattr(cfg, "migrate_config", lambda: None)

    assert cli.cmd_update(argparse.Namespace(check=False)) == 0
    assert "up to date" in capsys.readouterr().out


def test_update_fails_closed_when_github_is_unreachable(monkeypatch, capsys):
    import subprocess

    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_latest_github_version", lambda: None)
    monkeypatch.setattr(
        subprocess,
        "call",
        lambda _command: (_ for _ in ()).throw(AssertionError("must not install")),
    )

    assert cli.cmd_update(argparse.Namespace(check=False)) == 1
    assert "GitHub Releases" in capsys.readouterr().out
