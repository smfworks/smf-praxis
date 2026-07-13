# Install Praxis

## Recommended (from source)

```bash
git clone https://github.com/smfworks/smf-praxis.git
cd smf-praxis
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
praxis --version
praxis onboard              # or: praxis model set ollama/<model>
praxis daemon start
# open http://127.0.0.1:8643/
```

One-liner bootstrap (clones + installs + onboard):

```bash
curl -fsSL https://raw.githubusercontent.com/smfworks/smf-praxis/main/install.sh | bash
```

## From a GitHub Release

Replace `X.Y.Z` with the selected release version:

```bash
VERSION=X.Y.Z
pip install "https://github.com/smfworks/smf-praxis/releases/download/v${VERSION}/praxis_agent-${VERSION}-py3-none-any.whl"
praxis onboard
praxis daemon start
```

The release workflow currently publishes wheel and sdist artifacts to GitHub
Releases only; PyPI publication is disabled. See [RELEASING.md](../RELEASING.md).
Alternatively, install from git:

```bash
pip install "git+https://github.com/smfworks/smf-praxis.git@main"
```

## Optional professional artifact renderers

Canonical JSON and Markdown generation, validation, versioning, comparison, and
release-bundle verification are part of the dependency-free core. Install the
`artifacts` extra from a source checkout for DOCX, PDF, PPTX, and XLSX output:

```bash
pip install -e ".[artifacts]"
# development + rich artifacts
pip install -e ".[dev,artifacts]"
```

The extra installs `python-docx`, `reportlab`, `python-pptx`, `openpyxl`, Pillow,
and `pypdf`. See [Artifact Studio](artifacts/README.md).

## After install — three jobs

```bash
praxis jobs list
praxis jobs run research --query "Summarize open-source agent runtimes"
praxis budget set 5          # USD hard-stop (optional but recommended)
praxis doctor                # readiness incl. sandbox + budget
```

## Trust defaults

- **Sandbox:** `agents.sandbox.backend` defaults to **`auto`** (Docker when the
  Docker daemon is available, else local path-confined).
- **Budget:** unset = unlimited; set a cap with `praxis budget set <usd>` so
  chat, research, submit, and agent runs **hard-stop** when spent ≥ limit.
