"""Docs must describe the open-core tree, not the pre-extraction 0.28.x product."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_quickstart_does_not_activate_extracted_packs():
    text = (ROOT / "docs" / "QUICKSTART.md").read_text()
    assert "pack activate homeschool" not in text
    assert "30/30" in text


def test_readme_does_not_claim_homeschool_ships():
    text = (ROOT / "README.md").read_text()
    assert "bundled **general** and\n**homeschool**" not in text
    assert "30/30" in text


def test_releasing_describes_open_core_eval_count():
    text = (ROOT / "RELEASING.md").read_text()
    assert "36/36" not in text
    assert "30/30" in text
