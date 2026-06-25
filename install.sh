#!/usr/bin/env bash
# Praxis one-command installer + configurator (Linux / macOS).
#
# Single command, from scratch:
#   curl -fsSL https://raw.githubusercontent.com/smfworks/smf-praxis/main/install.sh | bash
#
# Or from a clone:
#   ./install.sh                      # core install + interactive onboarding
#   ./install.sh --with docs          # also install the document-parser extra
#   ./install.sh --with docs,multimodal,fast
#   ./install.sh --no-configure       # install only; skip onboarding
#   ./install.sh --provider ollama --model llama3.1   # non-interactive configure
#
# What it does: finds python3 -> creates a .venv -> installs Praxis -> runs the
# onboarding wizard (provider + model). The core install is dependency-free.
set -euo pipefail

REPO_URL="https://github.com/smfworks/smf-praxis.git"
VENV_DIR=".venv"
EXTRAS=""
DO_CONFIGURE=1
PROVIDER=""
MODEL=""
EDITABLE=""

while [ $# -gt 0 ]; do
  case "$1" in
    --with) EXTRAS="$2"; shift 2 ;;
    --with=*) EXTRAS="${1#*=}"; shift ;;
    --venv) VENV_DIR="$2"; shift 2 ;;
    --venv=*) VENV_DIR="${1#*=}"; shift ;;
    --no-configure) DO_CONFIGURE=0; shift ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --provider=*) PROVIDER="${1#*=}"; shift ;;
    --model) MODEL="$2"; shift 2 ;;
    --model=*) MODEL="${1#*=}"; shift ;;
    --editable|-e) EDITABLE="-e"; shift ;;
    -h|--help)
      sed -n '2,18p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done

say() { printf '\033[1;36m[praxis]\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m[praxis] %s\033[0m\n' "$*" >&2; exit 1; }

# 1. Locate a usable Python (>= 3.10).
PYBIN=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3,10) else 1)' 2>/dev/null; then
      PYBIN="$cand"; break
    fi
  fi
done
[ -n "$PYBIN" ] || die "Python >= 3.10 not found. Install it and re-run."
say "using $("$PYBIN" --version 2>&1)"

# 2. Resolve the project root. If run from inside a clone we install from here;
#    if piped (curl | bash) with no project nearby, clone it first.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
  PROJECT_DIR="$SCRIPT_DIR"
elif [ -f "./pyproject.toml" ]; then
  PROJECT_DIR="$(pwd)"
else
  command -v git >/dev/null 2>&1 || die "git is required to fetch Praxis. Install git and re-run."
  PROJECT_DIR="$(pwd)/smf-praxis"
  if [ -d "$PROJECT_DIR/.git" ]; then
    say "updating existing clone in $PROJECT_DIR"
    git -C "$PROJECT_DIR" pull --ff-only
  else
    say "cloning $REPO_URL"
    git clone --depth 1 "$REPO_URL" "$PROJECT_DIR"
  fi
fi
say "project: $PROJECT_DIR"

# 3. Create the virtual environment.
cd "$PROJECT_DIR"
if [ ! -d "$VENV_DIR" ]; then
  say "creating virtualenv: $VENV_DIR"
  "$PYBIN" -m venv "$VENV_DIR"
fi
VENV_PY="$VENV_DIR/bin/python"
[ -x "$VENV_PY" ] || VENV_PY="$VENV_DIR/Scripts/python.exe"   # MSYS/Git-Bash on Windows

# 4. Install Praxis (core is dependency-free; extras are opt-in).
say "upgrading pip"
"$VENV_PY" -m pip install --quiet --upgrade pip
TARGET="."
[ -n "$EXTRAS" ] && TARGET=".[$EXTRAS]"
say "installing praxis ${EDITABLE:+(editable) }$TARGET"
"$VENV_PY" -m pip install $EDITABLE "$TARGET"

# 5. Smoke-test the install, then configure.
say "verifying install"
"$VENV_PY" -m hybridagent.cli demo >/dev/null && say "demo OK"

if [ "$DO_CONFIGURE" -eq 1 ]; then
  if [ -n "$PROVIDER" ] && [ -n "$MODEL" ]; then
    say "configuring (non-interactive): $PROVIDER/$MODEL"
    "$VENV_PY" -m hybridagent.cli onboard --provider "$PROVIDER" --model "$MODEL"
  elif [ -t 0 ]; then
    say "starting onboarding wizard"
    "$VENV_PY" -m hybridagent.cli onboard
  else
    say "non-interactive shell; skipping onboarding."
    say "configure later with: $VENV_DIR/bin/praxis onboard"
  fi
fi

cat <<EOF

$(printf '\033[1;32m[praxis] ready.\033[0m')

  Activate the environment:   source $PROJECT_DIR/$VENV_DIR/bin/activate
  Try the demo:               praxis demo
  Configure a model:          praxis onboard
  Run a task:                 praxis handle "Prepare a customer follow-up email"

Offline by default (deterministic mock LLM); point at a real provider with
'praxis onboard'. Docs: $PROJECT_DIR/README.md
EOF
