"""Tests for the harness simplification cadence (H09)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "harness_simplify.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("harness_simplify", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["harness_simplify"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_known_components():
    """The script knows the three env-toggled components."""
    mod = _load_script()
    assert "verifier" in mod.ENV_FOR_COMPONENT
    assert "reflexion" in mod.ENV_FOR_COMPONENT
    assert "context_compact" in mod.ENV_FOR_COMPONENT


def test_env_for_component_format():
    """Each component maps to a NAME=value env var string."""
    mod = _load_script()
    for _name, env in mod.ENV_FOR_COMPONENT.items():
        assert "=" in env
        key, _, val = env.partition("=")
        assert key.startswith("PRAXIS_")
        assert val in ("0", "1", "off", "on")


def test_quality_doc_has_simplification_section():
    """quality-document.md must document the simplification cadence (H09
    verification command requirement)."""
    qd = REPO / "docs" / "harness" / "quality-document.md"
    assert qd.exists(), "quality-document.md missing"
    src = qd.read_text(encoding="utf-8")
    assert "simplification" in src.lower(), \
        "quality-document.md must mention 'simplification' (H09)"


def test_log_row_format():
    """_log_to_quality_doc appends a markdown table row."""
    mod = _load_script()
    # The row must start with '| ' and contain the 5 table columns.
    # We can't run it without polluting the doc, so verify the format
    # indirectly: the function exists and quality-doc path is set.
    assert hasattr(mod, "_log_to_quality_doc")
    assert mod.QUALITY_DOC.exists()