"""Config + credential storage, mirroring OpenClaw's layout.

Files (under ``~/.praxis/`` by default; override with ``PRAXIS_HOME``):
    praxis.json            -> { "agents": { "defaults": { "model": "provider/model" } },
                               "providers": { "<id>": { baseUrl, compatibility, keyRef } } }
    auth-profiles.json     -> { "<id>": { "apiKey": "..." } }   (plaintext, gitignored)

API keys default to an **env reference** (``keyRef.source == "env"``) so secrets
never land in the repo; "paste now" stores them in auth-profiles.json instead.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

APP = "praxis"
ENV_HOME = "PRAXIS_HOME"


def home_dir() -> Path:
    base = os.environ.get(ENV_HOME)
    return Path(base) if base else Path.home() / f".{APP}"


def config_path() -> Path:
    return home_dir() / f"{APP}.json"


def auth_path() -> Path:
    return home_dir() / "auth-profiles.json"


def load_config() -> dict:
    p = config_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_config(cfg: dict) -> Path:
    home_dir().mkdir(parents=True, exist_ok=True)
    p = config_path()
    p.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return p


def _load_auth() -> dict:
    p = auth_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_api_key(provider_id: str, api_key: str) -> None:
    home_dir().mkdir(parents=True, exist_ok=True)
    auth = _load_auth()
    auth.setdefault(provider_id, {})["apiKey"] = api_key
    auth_path().write_text(json.dumps(auth, indent=2), encoding="utf-8")
    try:  # best-effort: tighten perms on POSIX
        os.chmod(auth_path(), 0o600)
    except OSError:
        pass


def write_provider(provider_id: str, base_url: str, compatibility: str,
                   model_ref: str, key_env: str | None,
                   use_env_ref: bool = True) -> Path:
    """Persist provider + default model in OpenClaw-style config shape."""
    cfg = load_config()
    cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = model_ref
    providers = cfg.setdefault("providers", {})
    entry = {"baseUrl": base_url, "compatibility": compatibility}
    if key_env:
        if use_env_ref:
            entry["keyRef"] = {"source": "env", "id": key_env}
        else:
            entry["keyRef"] = {"source": "auth-profile", "id": provider_id}
    providers[provider_id] = entry
    return save_config(cfg)


def get_default_model() -> str | None:
    return (load_config().get("agents", {}).get("defaults", {}).get("model"))


def get_embed_model() -> str | None:
    """Optional separate embedding model ref ('provider/model'); None -> mock."""
    return (load_config().get("agents", {}).get("defaults", {}).get("embedModel"))


def set_embed_model(model_ref: str) -> Path:
    cfg = load_config()
    cfg.setdefault("agents", {}).setdefault("defaults", {})["embedModel"] = model_ref
    return save_config(cfg)


def split_model_ref(ref: str) -> tuple[str, str]:
    """'openrouter/openai/gpt-4o-mini' -> ('openrouter', 'openai/gpt-4o-mini')."""
    provider, _, model = ref.partition("/")
    return provider, model


def provider_entry(provider_id: str) -> dict | None:
    return load_config().get("providers", {}).get(provider_id)


def resolve_api_key(provider_id: str) -> str | None:
    """Resolve a key from env ref or stored auth profile (env wins)."""
    entry = provider_entry(provider_id) or {}
    ref = entry.get("keyRef")
    if ref:
        if ref.get("source") == "env":
            return os.environ.get(ref.get("id", ""))
        if ref.get("source") == "auth-profile":
            return _load_auth().get(provider_id, {}).get("apiKey")
    # Fall back to a stored key if present.
    return _load_auth().get(provider_id, {}).get("apiKey")


def is_configured() -> bool:
    return get_default_model() is not None
