"""Installed vertical distribution discovery and pack integration."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hybridagent import pack
from hybridagent.broker import RiskClass
from hybridagent.verticals import registry


class _EntryPoints(list):
    def select(self, *, group: str):
        return self if group == "praxis.verticals" else []


class _EntryPoint:
    def __init__(self, name: str, value: str, hook):
        self.name = name
        self.value = value
        self._hook = hook
        self.dist = None

    def load(self):
        return self._hook


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    registry.clear_registry()
    monkeypatch.setattr(registry.metadata, "entry_points", lambda: _EntryPoints())
    yield
    registry.clear_registry()


def _write_pack(root: Path, name: str, *, marker: str) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "pack.json").write_text(
        json.dumps(
            {
                "name": name,
                "version": "0.1.0",
                "systemPrompt": f"{marker} persona",
                "complianceMode": "enforced",
                "riskPolicy": {
                    "autonomousRisks": ["read"],
                    "dualApprovalRisks": ["send", "destructive"],
                },
            }
        ),
        encoding="utf-8",
    )
    return directory


def test_registered_pack_root_is_discoverable_and_user_override_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("PRAXIS_HOME", str(tmp_path / "home"))
    external_root = tmp_path / "external"
    external = _write_pack(external_root, "paid", marker="external")
    registry.register_vertical_pack_root(external_root)

    loaded = pack.load_pack("paid")
    assert loaded is not None
    assert Path(loaded.path) == external
    assert "external" in loaded.system_prompt
    assert pack.list_packs()["paid"].path == str(external)

    user_root = pack.packs_dir()
    user = _write_pack(user_root, "paid", marker="user")
    overridden = pack.load_pack("paid")
    assert overridden is not None
    assert Path(overridden.path) == user
    assert "user" in overridden.system_prompt


def test_bare_pack_name_wins_over_same_named_working_directory(tmp_path, monkeypatch):
    external_root = tmp_path / "installed"
    external = _write_pack(external_root, "collision", marker="installed")
    registry.register_vertical_pack_root(external_root)
    unrelated = tmp_path / "cwd" / "collision"
    unrelated.mkdir(parents=True)
    monkeypatch.chdir(unrelated.parent)

    loaded = pack.load_pack("collision")

    assert loaded is not None
    assert Path(loaded.path) == external


def test_entry_point_autoload_is_idempotent_and_registers_pack_root(tmp_path, monkeypatch):
    calls: list[str] = []
    root = tmp_path / "distribution-packs"
    _write_pack(root, "paid", marker="entrypoint")

    def hook() -> None:
        calls.append("called")
        registry.register_vertical_spec(
            registry.VerticalSpec(
                name="paid",
                persona_keyword="entrypoint",
                compliance_mode="enforced",
                autonomous={RiskClass.READ},
                held={RiskClass.SEND, RiskClass.DESTRUCTIVE},
                version="0.1.0",
            )
        )
        registry.register_vertical_pack_root(root)

    ep = _EntryPoint("paid", "example_vertical:register", hook)
    monkeypatch.setattr(registry.metadata, "entry_points", lambda: _EntryPoints([ep]))

    assert registry.load_installed_verticals() == {}
    assert registry.load_installed_verticals() == {}
    assert calls == ["called"]
    assert registry.get_vertical_spec("paid") is not None
    assert list(registry.iter_vertical_pack_roots()) == [root.resolve()]


def test_broken_installed_vertical_becomes_a_failing_eval(monkeypatch):
    def broken_hook() -> None:
        raise RuntimeError("registration exploded")

    ep = _EntryPoint("broken", "broken_vertical:register", broken_hook)
    monkeypatch.setattr(registry.metadata, "entry_points", lambda: _EntryPoints([ep]))

    from hybridagent.vertical_evals import vertical_eval_cases

    cases = vertical_eval_cases()
    failures = [case for case in cases if case.id == "vertical.broken.registration"]
    assert len(failures) == 1
    result = failures[0].evaluate()
    assert not result.passed
    assert "registration exploded" in result.detail
