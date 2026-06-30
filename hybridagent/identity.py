"""Agent identity + signed attestations (Phase B / G8).

Gives each Praxis agent a stable cryptographic identity so its actions and
inter-agent messages can be **attributed and tamper-checked**. This is the
foundation for zero-trust agent identity (cf. the Microsoft agent-governance
toolkit's SPIFFE/Ed25519 model) adapted to Praxis's dependency-free constraint.

Design:
* Default backend is **HMAC-SHA256** (stdlib only): a per-agent secret key signs
  canonical attestations; any holder of the key can verify. Sound for
  single-deployment attribution and tamper detection.
* If the optional ``cryptography`` package is present, an **Ed25519** asymmetric
  backend is used instead, enabling public verification without sharing a secret
  (true zero-trust across parties). Selected automatically; no API change.

An *attestation* is a signed statement ``{agent_id, action, args_hash, ts, nonce}``
the broker can emit per decision, producing a verifiable audit chain. Keys live in
``~/.praxis/identity.json`` (0600), never in world-readable config.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config as cfg

try:  # optional asymmetric upgrade
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import (
        Ed25519PrivateKey,
        Ed25519PublicKey,
    )
    _HAVE_ED25519 = True
except Exception:  # noqa: BLE001
    _HAVE_ED25519 = False


def _identity_path() -> Path:
    return Path(cfg.home_dir()) / "identity.json"


def _canonical(payload: dict) -> bytes:
    """Deterministic byte encoding for signing (sorted keys, no whitespace)."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()


@dataclass
class Attestation:
    agent_id: str
    action: str
    args_hash: str
    ts: float
    nonce: str
    algo: str
    signature: str

    def payload(self) -> dict:
        return {"agent_id": self.agent_id, "action": self.action,
                "args_hash": self.args_hash, "ts": self.ts, "nonce": self.nonce,
                "algo": self.algo}

    def to_dict(self) -> dict:
        d = self.payload()
        d["signature"] = self.signature
        return d


@dataclass
class AgentIdentity:
    agent_id: str
    algo: str  # "hmac-sha256" | "ed25519"
    _secret: bytes = field(repr=False, default=b"")
    _public: str = ""   # hex public key (ed25519) or "" (hmac)

    # ----------------------------------------------------------- persistence
    @classmethod
    def load_or_create(cls, agent_id: str = "praxis") -> "AgentIdentity":
        path = _identity_path()
        if path.exists():
            try:
                data = json.loads(path.read_text())
                rec = data.get(agent_id)
                if rec:
                    return cls(
                        agent_id=agent_id, algo=rec["algo"],
                        _secret=bytes.fromhex(rec.get("secret", "")),
                        _public=rec.get("public", ""))
            except Exception:  # noqa: BLE001 - corrupt file -> regenerate
                pass
        ident = cls._generate(agent_id)
        ident._persist()
        return ident

    @classmethod
    def _generate(cls, agent_id: str) -> "AgentIdentity":
        if _HAVE_ED25519:
            sk = Ed25519PrivateKey.generate()
            raw = sk.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption())
            pub = sk.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw)
            return cls(agent_id=agent_id, algo="ed25519",
                       _secret=raw, _public=pub.hex())
        return cls(agent_id=agent_id, algo="hmac-sha256",
                   _secret=secrets.token_bytes(32), _public="")

    def _persist(self) -> None:
        path = _identity_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except Exception:  # noqa: BLE001
                data = {}
        data[self.agent_id] = {"algo": self.algo, "secret": self._secret.hex(),
                               "public": self._public}
        path.write_text(json.dumps(data, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    # ----------------------------------------------------------------- signing
    @property
    def public_id(self) -> str:
        """A shareable identity fingerprint (public key for ed25519, key-hash
        for hmac so the secret is never exposed)."""
        if self.algo == "ed25519":
            return self._public
        return hashlib.sha256(b"praxis-id:" + self._secret).hexdigest()[:32]

    def _sign_bytes(self, msg: bytes) -> str:
        if self.algo == "ed25519" and _HAVE_ED25519:
            sk = Ed25519PrivateKey.from_private_bytes(self._secret)
            return sk.sign(msg).hex()
        return hmac.new(self._secret, msg, hashlib.sha256).hexdigest()

    def attest(self, action: str, args: dict | None = None) -> Attestation:
        args_hash = hashlib.sha256(_canonical(args or {})).hexdigest()
        payload = {"agent_id": self.agent_id, "action": action,
                   "args_hash": args_hash, "ts": time.time(),
                   "nonce": secrets.token_hex(8), "algo": self.algo}
        sig = self._sign_bytes(_canonical(payload))
        return Attestation(signature=sig, **payload)

    def verify(self, att: Attestation) -> bool:
        """Verify an attestation this identity signed (or, for ed25519, that
        anyone signed given the public key)."""
        msg = _canonical(att.payload())
        if att.algo == "ed25519" and _HAVE_ED25519 and self._public:
            try:
                pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(self._public))
                pk.verify(bytes.fromhex(att.signature), msg)
                return True
            except Exception:  # noqa: BLE001
                return False
        expected = hmac.new(self._secret, msg, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, att.signature)


def verify_with_public_key(att: Attestation, public_hex: str) -> bool:
    """Stateless ed25519 verification from a published public key (zero-trust:
    no shared secret). Returns False for hmac attestations (no public verify)."""
    if att.algo != "ed25519" or not _HAVE_ED25519:
        return False
    try:
        pk = Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_hex))
        pk.verify(bytes.fromhex(att.signature), _canonical(att.payload()))
        return True
    except Exception:  # noqa: BLE001
        return False
