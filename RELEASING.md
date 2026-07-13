# Releasing

Praxis publishes release artifacts to GitHub Releases as **`praxis-agent`**. The
current workflow does not publish to PyPI. The version is single-sourced from
`hybridagent/__init__.py` (`__version__`) and read into package metadata through
`[tool.setuptools.dynamic]` in `pyproject.toml`.

## Cut a release

1. Bump `__version__` in `hybridagent/__init__.py` using semantic versioning.
2. Commit the release candidate, complete required review and CI, and merge it to
   `main`.
3. Tag the verified merge commit and push the tag:

   ```bash
   git checkout main
   git pull --ff-only origin main
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

4. The **Release** workflow (`.github/workflows/release.yml`) builds the sdist and
   wheel, runs `twine check`, verifies dashboard assets and the tag/version match,
   then attaches both artifacts to the GitHub Release. `workflow_dispatch` runs
   the same build as a dry run without creating a release.
5. Verify the GitHub Release contains both:

   - `praxis_agent-X.Y.Z-py3-none-any.whl`
   - `praxis_agent-X.Y.Z.tar.gz`

Install the released wheel directly:

```bash
pip install https://github.com/smfworks/smf-praxis/releases/download/vX.Y.Z/praxis_agent-X.Y.Z-py3-none-any.whl
```

## PyPI status

PyPI publishing is intentionally disabled. No API token is stored, and the
release workflow has no PyPI publish action. `pip install praxis-agent` therefore
does not install the latest GitHub-only release.

To enable PyPI later:

1. Create or claim the `praxis-agent` project on PyPI.
2. Configure either a PyPI Trusted Publisher for `smfworks/smf-praxis` and
   `.github/workflows/release.yml`, or a scoped API token stored as a GitHub
   Actions secret.
3. Add `pypa/gh-action-pypi-publish` to the tag-only publish job with the required
   `id-token: write` permission for Trusted Publishing.
4. Update this document and add an automated release-contract test before
   enabling publication.

## Local dry-run

```bash
bash scripts/verify-release.sh
```

The verifier builds the sdist and wheel, runs `twine check`, confirms dashboard
assets and nested Python packages are present, installs the wheel into a clean
virtual environment from a neutral directory, attests that `hybridagent` resolves
inside that environment rather than the checkout, imports every vertical
authority module, and checks `praxis --version`. It does not publish.

## Build and inspect locally

```bash
python -m pip install --upgrade build twine
python -m build
twine check dist/*
python -c "import glob,zipfile; print(len([n for n in zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist() if '/web/' in n]), 'web files')"
```

The `web/` JavaScript and CSS are package data declared in
`[tool.setuptools.package-data]`. The daemon serves them from
`Path(__file__).parent / "web"`, so they must ship in the wheel; editable installs
can mask a missing-package-data defect by pointing back to the checkout.
