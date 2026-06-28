# Releasing

Praxis publishes to PyPI as **`praxis-agent`**. The version is single-sourced from
`hybridagent/__init__.py` (`__version__`) and read into the package metadata via
`[tool.setuptools.dynamic]` in `pyproject.toml`.

## Cut a release

1. Bump `__version__` in `hybridagent/__init__.py` (semantic versioning).
2. Commit it, then tag and push:

   ```bash
   git commit -am "Release vX.Y.Z"
   git tag vX.Y.Z
   git push origin main --tags
   ```

3. The **Release** workflow (`.github/workflows/release.yml`) builds the sdist +
   wheel, runs `twine check`, verifies the wheel bundles the dashboard assets
   (`hybridagent/web/*`) and that the tag matches `__version__`, then publishes to
   PyPI. `workflow_dispatch` runs the same build **without** publishing (dry run).

## One-time PyPI setup (Trusted Publishing)

No API token is stored. Configure a Trusted Publisher once on PyPI:

1. Create/own the `praxis-agent` project on https://pypi.org.
2. Project → **Settings → Publishing → Add a trusted publisher** (GitHub Actions):
   - Owner: `smfworks` · Repository: `smf-praxis`
   - Workflow: `release.yml` · Environment: `pypi`
3. In GitHub, create a repo **Environment** named `pypi` (Settings → Environments).

## Build & verify locally

```bash
python -m pip install --upgrade build twine
python -m build
twine check dist/*
# confirm the dashboard assets are bundled:
python -c "import glob,zipfile;print(len([n for n in zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist() if '/web/' in n]),'web files')"
```

> The `web/` JS/CSS are **package data** (`[tool.setuptools.package-data]`). The
> daemon serves them from `Path(__file__).parent / "web"` at runtime, so they must
> ship in the wheel — otherwise a `pip`/`pipx` install has a broken dashboard
> (editable installs mask this by pointing back at the source tree).
