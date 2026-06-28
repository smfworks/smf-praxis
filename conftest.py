import os
import sys

import pytest

# Tests always use the offline mock LLM, regardless of any machine-level config.
os.environ.setdefault("PRAXIS_LLM", "mock")

sys.path.insert(0, os.path.dirname(__file__))


@pytest.fixture(autouse=True)
def _no_real_keychain(monkeypatch):
    """Keep the suite hermetic: never touch the real OS keychain even when the
    optional `keyring` extra is installed. Tests that exercise keychain behaviour
    install their own in-memory fake via monkeypatch (which overrides this)."""
    from hybridagent import config as cfg
    monkeypatch.setattr(cfg, "_keyring", lambda: None, raising=False)
