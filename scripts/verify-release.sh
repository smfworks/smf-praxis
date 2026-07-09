#!/usr/bin/env bash
# Local release dry-run: build sdist/wheel, twine check, asset audit, clean-venv install.
# Does NOT publish to PyPI. Mirrors .github/workflows/release.yml build job.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

echo "==> version"
python - <<'PY'
from hybridagent import __version__
print("hybridagent.__version__ =", __version__)
PY

echo "==> build"
python -m pip install -q -U pip build twine
rm -rf dist build
python -m build
twine check dist/*

echo "==> wheel dashboard assets"
python - <<'PY'
import glob, sys, zipfile
whl = glob.glob("dist/*.whl")[0]
web = [n for n in zipfile.ZipFile(whl).namelist() if "/web/" in n]
print(f"dashboard assets in wheel: {len(web)}")
need = ("shell.js", "shell.css", "friendliness.js", "friendliness.css")
missing = [n for n in need if not any(n in x for x in web)]
if missing:
    print("MISSING", missing)
    sys.exit(1)
if not web:
    sys.exit(1)
print("ok:", ", ".join(sorted({x.rsplit("/", 1)[-1] for x in web})[:12]), "...")
PY

echo "==> clean venv install from wheel"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
python -m venv "$TMP/venv"
# shellcheck disable=SC1091
source "$TMP/venv/bin/activate"
pip install -q --upgrade pip
pip install -q dist/praxis_agent-*.whl
praxis --version
python - <<'PY'
from importlib.resources import files
from pathlib import Path
web = Path(files("hybridagent") / "web")
assert web.is_dir(), web
for name in ("shell.js", "shell.css", "friendliness.js"):
    p = web / name
    assert p.is_file(), p
print("package data web/ ok:", sorted(p.name for p in web.iterdir() if p.suffix in {".js", ".css"})[:8], "...")
from hybridagent.jobs import list_jobs
assert {j["id"] for j in list_jobs()} == {"research", "draft", "schedule"}
print("jobs catalog ok")
PY
deactivate

echo "==> PASS: release dry-run + install verification"
echo "    (PyPI publish still needs Trusted Publisher — see RELEASING.md)"
