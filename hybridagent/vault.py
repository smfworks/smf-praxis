"""Credential vault — named secret bundles with runtime injection (Phase D / G14).

Beyond per-provider LLM keys, agents need scoped credential sets for tools and
MCP servers (a GitHub token, an SMTP password, a customer API key). This vault
stores **named bundles** of secrets and injects them as environment variables
only for the duration of a call, so:

* secrets live in ``~/.praxis/vault.json`` at **0600**, never in world-readable
  ``praxis.json`` and never echoed back by status views;
* a bundle is **scoped**: it declares which tools it applies to, so a credential
  for the email tool isn't exposed to an unrelated web fetch;
* injection is **ephemeral** — env vars are set around the call and restored
  afterward (``with vault.inject(bundle):``), never written to the agent's
  persistent environment.

Stdlib only. Values are stored obfuscated (base64) at rest by default — this is
*not* encryption (documented honestly), it prevents shoulder-surfing/log-scrape,
and the file mode is the real boundary. An optional ``cryptography`` Fernet path
activates automatically when a ``PRAXIS_VAULT_KEY`` env var is set.
"""
from __future__ import annotations

import base64
import contextlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import config as cfg
from .logging_util import get_logger

_log = get_logger("praxis.vault")


def _vault_path() -> Path:
    return Path(cfg.home_dir()) / "vault.json"


def _fernet():
    """Return a Fernet cipher if cryptography + PRAXIS_VAULT_KEY are present."""
    key = os.environ.get("PRAXIS_VAULT_KEY")
    if not key:
        return None
    try:
        from cryptography.fernet import Fernet
        # derive a urlsafe 32-byte key from the provided secret
        import hashlib
        digest = hashlib.sha256(key.encode()).digest()
        return Fernet(base64.urlsafe_b64encode(digest))
    except ImportError:
        # The user set PRAXIS_VAULT_KEY expecting encryption, but cryptography
        # isn't installed. Do NOT silently downgrade to base64 (which is not
        # encryption) without telling them — that's a false security assurance.
        _log.warning("PRAXIS_VAULT_KEY is set but the 'cryptography' package is "
                     "not installed; vault values are stored base64-OBFUSCATED, "
                     "NOT encrypted. Install cryptography for real encryption.")
        return None
    except Exception:  # noqa: BLE001
        return None


def _enc(value: str) -> str:
    f = _fernet()
    if f is not None:
        return "f:" + f.encrypt(value.encode()).decode()
    return "b:" + base64.b64encode(value.encode()).decode()


def _dec(stored: str) -> str:
    if stored.startswith("f:"):
        f = _fernet()
        if f is None:
            raise ValueError("bundle is Fernet-encrypted but PRAXIS_VAULT_KEY is unset")
        return f.decrypt(stored[2:].encode()).decode()
    if stored.startswith("b:"):
        return base64.b64decode(stored[2:].encode()).decode()
    return stored  # legacy/plaintext tolerance


@dataclass
class Bundle:
    name: str
    scope: list[str]          # tool names this bundle applies to ([] = all)
    keys: list[str]           # env var names (values never exposed)


class CredentialVault:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or _vault_path()

    # ----------------------------------------------------------- persistence
    def _read(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _write(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    # ---------------------------------------------------------------- mutate
    def put(self, name: str, values: dict, scope: list[str] | None = None) -> Bundle:
        """Create/replace a named bundle of secret env vars."""
        data = self._read()
        data[name] = {
            "scope": scope or [],
            "values": {k: _enc(str(v)) for k, v in values.items()},
        }
        self._write(data)
        _log.info("stored credential bundle '%s' (%d keys, scope=%s)",
                  name, len(values), scope or "all")
        return Bundle(name=name, scope=scope or [], keys=sorted(values))

    def delete(self, name: str) -> bool:
        data = self._read()
        if name not in data:
            return False
        del data[name]
        self._write(data)
        return True

    # ------------------------------------------------------------------ read
    def list(self) -> list[Bundle]:
        return [Bundle(name=n, scope=b.get("scope", []),
                       keys=sorted(b.get("values", {})))
                for n, b in self._read().items()]

    def get(self, name: str) -> Bundle | None:
        b = self._read().get(name)
        if b is None:
            return None
        return Bundle(name=name, scope=b.get("scope", []),
                      keys=sorted(b.get("values", {})))

    def _resolve_values(self, name: str) -> dict:
        b = self._read().get(name)
        if b is None:
            return {}
        return {k: _dec(v) for k, v in b.get("values", {}).items()}

    def bundles_for_tool(self, tool: str) -> list[str]:
        """Names of bundles whose scope includes ``tool`` (or is unscoped)."""
        out = []
        for n, b in self._read().items():
            scope = b.get("scope", [])
            if not scope or tool in scope:
                out.append(n)
        return out

    # -------------------------------------------------------------- injection
    @contextlib.contextmanager
    def inject(self, *bundle_names: str):
        """Temporarily set a bundle's env vars for the duration of the block,
        then restore the prior environment. Secrets never persist in os.environ
        beyond the call."""
        saved: dict[str, str | None] = {}
        try:
            for name in bundle_names:
                for k, v in self._resolve_values(name).items():
                    saved.setdefault(k, os.environ.get(k))
                    os.environ[k] = v
            yield
        finally:
            for k, prev in saved.items():
                if prev is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = prev


def inject_for_tool(tool: str):
    """Context manager that injects every in-scope bundle for ``tool``."""
    vault = CredentialVault()
    return vault.inject(*vault.bundles_for_tool(tool))
