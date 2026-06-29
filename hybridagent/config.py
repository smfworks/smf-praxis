"""Config + credential storage.

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


# --- schema migrations -------------------------------------------------------
# Bump CONFIG_VERSION when praxis.json's shape changes and append a migration.
CONFIG_VERSION = 1


def _migrate_v0_to_v1(cfg: dict) -> dict:
    """Baseline: stamp pre-versioned configs. No structural change yet — this
    establishes the upgrade path so later versions can transform old configs."""
    return cfg


# Indexed by from-version: _CONFIG_MIGRATIONS[0] upgrades v0 -> v1, etc.
_CONFIG_MIGRATIONS = [_migrate_v0_to_v1]


def migrate_config() -> int | None:
    """Upgrade praxis.json to CONFIG_VERSION, applying each migration in order.

    Returns the new version when a migration ran, else None. Cheap to call on
    every startup — an already-current config is a single read with no write."""
    p = config_path()
    if not p.exists():
        return None
    data = load_config()
    ver = int(data.get("configVersion", 0))
    if ver >= CONFIG_VERSION:
        return None
    for from_ver in range(ver, CONFIG_VERSION):
        data = _CONFIG_MIGRATIONS[from_ver](data)
    data["configVersion"] = CONFIG_VERSION
    save_config(data)
    return CONFIG_VERSION


def _load_auth() -> dict:
    p = auth_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


# --- secret storage: OS keychain (optional `keyring` extra) with file fallback ---
KEYCHAIN_SERVICE = "praxis-agent"


def _keyring():
    """Return the keyring module when a usable backend is present, else None."""
    try:
        import keyring
        from keyring.backends.fail import Keyring as _Fail
        if isinstance(keyring.get_keyring(), _Fail):
            return None
        return keyring
    except Exception:
        return None


def keychain_available() -> bool:
    return _keyring() is not None


def keychain_get(provider_id: str) -> str | None:
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(KEYCHAIN_SERVICE, provider_id)
    except Exception:
        return None


def keychain_set(provider_id: str, api_key: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(KEYCHAIN_SERVICE, provider_id, api_key)
        return True
    except Exception:
        return False


def keychain_delete(provider_id: str) -> bool:
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.delete_password(KEYCHAIN_SERVICE, provider_id)
        return True
    except Exception:
        return False


def _write_file_key(provider_id: str, api_key: str) -> None:
    home_dir().mkdir(parents=True, exist_ok=True)
    auth = _load_auth()
    auth.setdefault(provider_id, {})["apiKey"] = api_key
    auth_path().write_text(json.dumps(auth, indent=2), encoding="utf-8")
    try:  # best-effort: tighten perms on POSIX
        os.chmod(auth_path(), 0o600)
    except OSError:
        pass


def _delete_file_key(provider_id: str) -> None:
    p = auth_path()
    if not p.exists():
        return
    auth = _load_auth()
    if provider_id not in auth:
        return
    auth.pop(provider_id, None)
    if auth:
        p.write_text(json.dumps(auth, indent=2), encoding="utf-8")
    else:
        try:
            p.unlink()
        except OSError:
            pass


def save_api_key(provider_id: str, api_key: str) -> str:
    """Store an API key in the OS keychain when available, else a gitignored
    plaintext file. Returns the backend used: 'keychain' or 'file'."""
    if keychain_set(provider_id, api_key):
        _delete_file_key(provider_id)   # promote off any stale plaintext copy
        return "keychain"
    _write_file_key(provider_id, api_key)
    return "file"


def delete_api_key(provider_id: str) -> None:
    """Remove a stored key from both the keychain and the plaintext file."""
    keychain_delete(provider_id)
    _delete_file_key(provider_id)


def migrate_secrets_to_keychain() -> int:
    """Move any plaintext file-stored keys into the OS keychain. Returns the count
    moved; a no-op when no keychain backend is available."""
    if not keychain_available():
        return 0
    auth = _load_auth()
    moved = 0
    for provider_id, rec in list(auth.items()):
        key = rec.get("apiKey") if isinstance(rec, dict) else None
        if key and keychain_set(provider_id, key):
            _delete_file_key(provider_id)
            moved += 1
    return moved


def key_location(provider_id: str) -> str:
    """Human label of where a provider's key resolves: env:NAME / keychain /
    file (plaintext) / not set."""
    entry = provider_entry(provider_id) or {}
    ref = entry.get("keyRef") or {}
    if ref.get("source") == "env":
        name = ref.get("id", "")
        return f"env:{name}" + ("" if os.environ.get(name) else " (unset)")
    if keychain_get(provider_id):
        return "keychain"
    if _load_auth().get(provider_id, {}).get("apiKey"):
        return "file (plaintext)"
    return "not set"


def write_provider(provider_id: str, base_url: str, compatibility: str,
                   model_ref: str, key_env: str | None,
                   use_env_ref: bool = True) -> Path:
    """Persist provider + default model in the standard config shape."""
    cfg = load_config()
    cfg.setdefault("agents", {}).setdefault("defaults", {})["model"] = model_ref
    providers = cfg.setdefault("providers", {})
    entry: dict = {"baseUrl": base_url, "compatibility": compatibility}
    if key_env:
        if use_env_ref:
            entry["keyRef"] = {"source": "env", "id": key_env}
        else:
            entry["keyRef"] = {"source": "auth-profile", "id": provider_id}
    providers[provider_id] = entry
    return save_config(cfg)


def get_default_model() -> str | None:
    explicit = load_config().get("agents", {}).get("defaults", {}).get("model")
    if explicit:
        return explicit
    # An active vertical pack may pin a model; the explicit config default wins.
    try:
        from . import pack
        return pack.resolve_model()
    except Exception:
        return None


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
    """Resolve a key: an env reference wins, otherwise the local secret store
    (OS keychain when available, then the gitignored plaintext file)."""
    entry = provider_entry(provider_id) or {}
    ref = entry.get("keyRef")
    if ref and ref.get("source") == "env":
        return os.environ.get(ref.get("id", ""))
    # auth-profile, or no/unknown ref: read the local secret store.
    return keychain_get(provider_id) or _load_auth().get(provider_id, {}).get("apiKey")


def is_configured() -> bool:
    return get_default_model() is not None


def get_active_pack_name() -> str | None:
    """Name of the active vertical pack, or None."""
    return load_config().get("activePack") or None


def set_active_pack_name(name: str | None) -> Path:
    cfg = load_config()
    if name:
        cfg["activePack"] = name
    else:
        cfg.pop("activePack", None)
    return save_config(cfg)


def get_voice_config() -> dict:
    """Operator-selected voice settings (agents.voice in praxis.json)."""
    return load_config().get("agents", {}).get("voice", {}) or {}


def set_voice_config(voice: dict) -> Path:
    cfg = load_config()
    cfg.setdefault("agents", {})["voice"] = voice
    return save_config(cfg)


def set_voice_mode(mode: str) -> Path:
    cfg = load_config()
    cfg.setdefault("agents", {}).setdefault("voice", {})["mode"] = mode
    return save_config(cfg)


def get_notify_config() -> dict:
    """Operator-selected notification settings (agents.notify in praxis.json)."""
    return load_config().get("agents", {}).get("notify", {}) or {}


def set_notify_config(notify: dict) -> Path:
    cfg = load_config()
    cfg.setdefault("agents", {})["notify"] = notify
    return save_config(cfg)
