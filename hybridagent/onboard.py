"""Interactive provider/model onboarding wizard.

Flow: existing-config detection (Keep/Modify/Reset) -> pick provider -> pick
model -> choose key storage (env reference by default, or paste now) -> write
config. Also supports a non-interactive path for scripts/CI.
"""
from __future__ import annotations

import os

from . import config as cfg
from .providers import CATALOG, ORDER, Provider, discover_ollama_models


def _choose(prompt: str, options: list[str], default_index: int = 0) -> int:
    print(f"\n{prompt}")
    for i, opt in enumerate(options):
        marker = " (default)" if i == default_index else ""
        print(f"  {i + 1}. {opt}{marker}")
    while True:
        raw = input(f"Enter 1-{len(options)} [{default_index + 1}]: ").strip()
        if not raw:
            return default_index
        if raw.isdigit() and 1 <= int(raw) <= len(options):
            return int(raw) - 1
        print("  Invalid choice, try again.")


def _model_options(provider: Provider, base_url: str,
                   api_key: str | None = None) -> list[str]:
    if provider.id in ("ollama", "ollama-cloud"):
        discovered = discover_ollama_models(base_url, api_key=api_key)
        if discovered:
            return discovered
        kind = "cloud" if provider.id == "ollama-cloud" else "local"
        print(f"  (No {kind} Ollama models discovered; showing common suggestions.)")
    return list(provider.suggested_models)


def run_noninteractive(provider_id: str, model: str,
                       base_url: str | None = None,
                       api_key: str | None = None,
                       use_env_ref: bool = True) -> dict:
    provider = CATALOG[provider_id]
    base = base_url or provider.base_url
    backend = None
    if api_key and not use_env_ref:
        backend = cfg.save_api_key(provider_id, api_key)
    model_ref = f"{provider_id}/{model}"
    cfg.write_provider(provider_id, base, provider.compatibility, model_ref,
                       provider.key_env, use_env_ref=use_env_ref)
    # First-run bootstrap: enable memory/skill recall defaults and seed a
    # starter knowledge namespace so the install is usable immediately.
    try:
        from . import bootstrap
        bootstrap.run()
    except Exception:
        pass
    return {"provider": provider_id, "model": model_ref, "base_url": base,
            "key_backend": backend}


def run() -> dict:
    """Interactive wizard. Returns the chosen config summary."""
    print("=" * 64)
    print(" Praxis setup — choose your model provider")
    print("=" * 64)

    # 1. Existing-config detection (Keep / Modify / Reset).
    if cfg.is_configured():
        current = cfg.get_default_model()
        action = _choose(
            f"Existing config found (model: {current}). What now?",
            ["Keep (exit)", "Modify (reconfigure)", "Reset (wipe + reconfigure)"],
            default_index=0,
        )
        if action == 0:
            print("Keeping existing configuration.")
            return {"model": current, "kept": True}
        if action == 2:
            p = cfg.config_path()
            if p.exists():
                p.unlink()

    # 2. Provider.
    labels = [CATALOG[pid].label for pid in ORDER]
    pidx = _choose("Pick a provider:", labels, default_index=0)
    provider = CATALOG[ORDER[pidx]]

    # 3. Base URL (custom only / allow override).
    base_url = provider.base_url
    if provider.id == "custom" or not base_url:
        base_url = input("Enter the OpenAI-compatible base URL "
                         "(e.g. https://host/v1): ").strip()
    if provider.notes:
        print(f"  note: {provider.notes}")

    # 4. Key storage (ask before model discovery so cloud Ollama can list models).
    use_env_ref = True
    api_key = None
    if provider.needs_key:
        mode = _choose(
            f"How should the API key be provided? ({provider.key_env})",
            [f"Environment variable reference ({provider.key_env})  [recommended]",
             "Paste the key now (stored locally, gitignored)"],
            default_index=0,
        )
        if mode == 0:
            api_key = os.environ.get(provider.key_env or "")
            if not api_key:
                print(f"  ⚠ {provider.key_env} is not set in this environment. "
                      f"Set it before running: setx {provider.key_env} <key> (Windows) "
                      f"or export {provider.key_env}=<key> (bash).")
        else:
            api_key = input(f"Paste {provider.key_env}: ").strip()
            use_env_ref = False

    # 5. Model (with key so cloud Ollama discovery works).
    options = _model_options(provider, base_url, api_key=api_key)
    options = options + ["(enter a model id manually)"]
    midx = _choose("Pick a model:", options, default_index=0)
    if midx == len(options) - 1:
        model = input("Enter model id: ").strip()
    else:
        model = options[midx]

    summary = run_noninteractive(provider.id, model, base_url, api_key, use_env_ref)
    print("\n" + "=" * 64)
    print(f" Configured: model = {summary['model']}")
    print(f" Config written to: {cfg.config_path()}")
    if provider.needs_key and use_env_ref:
        print(f" Key source: env var {provider.key_env}")
    elif provider.needs_key:
        if summary.get("key_backend") == "keychain":
            print(" Key source: OS keychain")
        else:
            print(f" Key source: {cfg.auth_path()} (gitignored)")
    print("=" * 64)
    return summary


if __name__ == "__main__":
    run()
