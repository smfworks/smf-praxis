import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_pyproject_discovers_all_hybridagent_subpackages():
    text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert not any(
        line.strip().startswith("packages =") for line in text.splitlines()
    )
    marker = "[tool.setuptools.packages.find]"
    assert marker in text
    section = text.split(marker, 1)[1].split("\n[", 1)[0]
    assert 'include = ["hybridagent*"]' in section

    nested_packages = {
        init.parent.relative_to(ROOT).as_posix()
        for init in (ROOT / "hybridagent").rglob("__init__.py")
        if init.parent != ROOT / "hybridagent"
    }
    assert "hybridagent/verticals/legal" in nested_packages
    assert "hybridagent/artifacts" in nested_packages
    assert len(nested_packages) >= 8


def test_release_verifier_uses_installed_package_outside_checkout():
    script = (ROOT / "scripts" / "verify-release.sh").read_text(encoding="utf-8")
    assert 'cd "$TMP"' in script
    assert "package_file.is_relative_to(venv)" in script
    assert "not package_file.is_relative_to(checkout)" in script
    for vertical in (
        "architecture",
        "dental",
        "education",
        "forensic_engineering",
        "legal",
        "medical",
    ):
        assert f"hybridagent.verticals.{vertical}.authority" in script
    assert "from hybridagent.artifacts import (" in script
    assert "ArtifactStudio," in script
    assert "render_artifact," in script


def test_release_workflow_attaches_wheel_and_sdist():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(
        encoding="utf-8"
    )
    release_step = workflow.split("- name: Attach wheel + sdist", 1)[1].split(
        "# NOTE:", 1
    )[0]
    assert "dist/*.whl" in release_step
    assert "dist/*.tar.gz" in release_step
    assert "publish the package to PyPI" not in workflow
    assert "PyPI publishing is intentionally not in this workflow" in workflow


def test_release_documentation_matches_github_only_distribution():
    documentation = (ROOT / "RELEASING.md").read_text(encoding="utf-8")
    assert "publishes release artifacts to GitHub Releases" in documentation
    assert "does not publish to PyPI" in documentation
    assert "releases/download/vX.Y.Z/praxis_agent-X.Y.Z-py3-none-any.whl" in documentation
    assert "then publishes to\n   PyPI" not in documentation


def test_installation_docs_do_not_recommend_unpublished_pypi_package():
    paths = (
        ROOT / "README.md",
        ROOT / "docs" / "INSTALL.md",
        ROOT / "docs" / "QUICKSTART.md",
        ROOT / "docs" / "DEPLOYMENT.md",
    )
    pypi_requirement = re.compile(
        r"\bpipx?\s+install\s+[\"']?praxis-agent(?:\[|\b)", re.IGNORECASE
    )
    for path in paths:
        text = path.read_text(encoding="utf-8")
        assert "## From PyPI" not in text
        assert "### From PyPI" not in text
        assert "latest PyPI release" not in text
        assert pypi_requirement.search(text) is None
        assert (
            "github.com/smfworks/smf-praxis/releases/download/" in text
            or "raw.githubusercontent.com/smfworks/smf-praxis/main/install" in text
        )
