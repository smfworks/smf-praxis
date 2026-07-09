#!/usr/bin/env bash
# Local release dry-run: build sdist/wheel, twine check, asset audit, clean-venv install.
# Does NOT publish to PyPI. Mirrors .github/workflows/release.yml build job.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-}"
if [[ -z "$PYTHON_BIN" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="python3"
  elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="python"
  else
    echo "error: neither python3 nor python is on PATH" >&2
    exit 1
  fi
fi

echo "==> version"
"$PYTHON_BIN" - <<'PY'
from hybridagent import __version__
print("hybridagent.__version__ =", __version__)
PY

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "==> build venv"
"$PYTHON_BIN" -m venv "$TMP/build-venv"
# shellcheck disable=SC1091
source "$TMP/build-venv/bin/activate"
python -m pip install -q -U pip build twine

echo "==> build"
rm -rf dist build
python -m build
twine check dist/*
deactivate

echo "==> wheel dashboard assets"
"$PYTHON_BIN" - <<'PY'
import glob, sys, zipfile
whl = glob.glob("dist/*.whl")[0]
web = [n for n in zipfile.ZipFile(whl).namelist() if "/web/" in n]
print(f"dashboard assets in wheel: {len(web)}")
need = ("shell.js", "shell.css", "friendliness.js", "friendliness.css", "cron.js", "cron.css")
missing = [n for n in need if not any(n in x for x in web)]
if missing:
    print("MISSING", missing)
    sys.exit(1)
if not web:
    sys.exit(1)
print("ok:", ", ".join(sorted({x.rsplit("/", 1)[-1] for x in web})[:12]), "...")
PY

echo "==> clean venv install from wheel"
"$PYTHON_BIN" -m venv "$TMP/install-venv"
# shellcheck disable=SC1091
source "$TMP/install-venv/bin/activate"
pip install -q --upgrade pip
pip install -q dist/praxis_agent-*.whl
praxis --version
python - <<'PY'
from importlib.resources import files
from pathlib import Path
web = Path(files("hybridagent") / "web")
assert web.is_dir(), web
for name in ("shell.js", "shell.css", "friendliness.js", "cron.js", "cron.css"):
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
