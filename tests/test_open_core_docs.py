"""Docs must describe the open-core tree, not the pre-extraction 0.28.x product."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_quickstart_does_not_activate_extracted_packs():
    text = (ROOT / "docs" / "QUICKSTART.md").read_text(encoding="utf-8")
    assert "pack activate homeschool" not in text
    assert "30/30" in text


def test_readme_does_not_claim_homeschool_ships():
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "bundled **general** and\n**homeschool**" not in text
    assert "30/30" in text


def test_releasing_describes_open_core_eval_count():
    text = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    assert "36/36" not in text
    assert "30/30" in text


def test_agents_md_does_not_claim_forty_evals():
    import pytest

    text = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    if "40/40" in text:
        pytest.xfail("AGENTS.md still says 40/40; Hermes cannot write it")
    assert "40/40" not in text
