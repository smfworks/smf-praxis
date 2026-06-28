"""Tests for `praxis update` and praxis.json schema migrations."""

import argparse

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
    monkeypatch.setattr(cli, "_latest_pypi_version", lambda: "999.0.0")
    rc = cli.cmd_update(argparse.Namespace(check=True))
    out = capsys.readouterr().out
    assert rc == 0 and "999.0.0" in out


def test_update_check_up_to_date(monkeypatch, capsys):
    from hybridagent import __version__
    monkeypatch.setattr(cli, "_is_editable_install", lambda: False)
    monkeypatch.setattr(cli, "_latest_pypi_version", lambda: __version__)
    rc = cli.cmd_update(argparse.Namespace(check=True))
    out = capsys.readouterr().out.lower()
    assert rc == 0 and "up to date" in out
