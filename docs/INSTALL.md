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

## From PyPI (`praxis-agent`)

```bash
pip install praxis-agent
praxis onboard
praxis daemon start
```

If `pip` does not show the latest GitHub tag, **Trusted Publishing** may not be
configured yet for the `smfworks/smf-praxis` repo (see [RELEASING.md](../RELEASING.md)).
Until that is fixed, install from git:

```bash
pip install "git+https://github.com/smfworks/smf-praxis.git@main"
```

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
