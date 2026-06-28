"""Tests for secret storage: OS keychain (optional) with gitignored file fallback."""

from hybridagent import config as cfg


class _FakeKeyring:
    """In-memory stand-in for the `keyring` module."""

    def __init__(self) -> None:
        self.store: dict = {}

    def get_password(self, service, name):
        return self.store.get((service, name))

    def set_password(self, service, name, value):
        self.store[(service, name)] = value

    def delete_password(self, service, name):
        self.store.pop((service, name), None)


def _home(tmp_path, monkeypatch):
    monkeypatch.setenv(cfg.ENV_HOME, str(tmp_path))


def test_save_and_resolve_via_keychain(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    fake = _FakeKeyring()    # one shared fake so set + get hit the same store
    monkeypatch.setattr(cfg, "_keyring", lambda: fake)
    assert cfg.save_api_key("openai", "sk-kc") == "keychain"
    assert cfg.resolve_api_key("openai") == "sk-kc"
    assert not cfg.auth_path().exists()          # nothing written in plaintext


def test_fallback_to_file_without_keychain(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "_keyring", lambda: None)
    assert cfg.save_api_key("openai", "sk-file") == "file"
    assert cfg.resolve_api_key("openai") == "sk-file"
    assert cfg.auth_path().exists()


def test_env_reference_wins(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "_keyring", lambda: None)
    monkeypatch.setenv("MYKEY", "sk-env")
    cfg.write_provider("openai", "https://api", "openai", "openai/gpt-4o",
                       "MYKEY", use_env_ref=True)
    cfg.save_api_key("openai", "sk-stored")      # also stored locally
    assert cfg.resolve_api_key("openai") == "sk-env"   # env ref still wins


def test_migrate_moves_file_to_keychain_and_scrubs(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    monkeypatch.setattr(cfg, "_keyring", lambda: None)
    cfg.save_api_key("openai", "sk-old")         # lands in the plaintext file
    assert cfg.auth_path().exists()
    fake = _FakeKeyring()
    monkeypatch.setattr(cfg, "_keyring", lambda: fake)
    assert cfg.migrate_secrets_to_keychain() == 1
    assert fake.store[(cfg.KEYCHAIN_SERVICE, "openai")] == "sk-old"
    assert cfg.resolve_api_key("openai") == "sk-old"
    assert "openai" not in cfg._load_auth()      # scrubbed from the file


def test_delete_removes_from_both(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(cfg, "_keyring", lambda: fake)
    cfg.save_api_key("openai", "sk-x")
    cfg.delete_api_key("openai")
    assert cfg.resolve_api_key("openai") is None


def test_key_location_labels(tmp_path, monkeypatch):
    _home(tmp_path, monkeypatch)
    fake = _FakeKeyring()
    monkeypatch.setattr(cfg, "_keyring", lambda: fake)
    monkeypatch.setenv("MYKEY", "sk-env")
    cfg.write_provider("openai", "https://api", "openai", "openai/gpt-4o",
                       "MYKEY", use_env_ref=True)
    assert cfg.key_location("openai").startswith("env:MYKEY")
    cfg.write_provider("anthropic", "https://api", "anthropic", "anthropic/claude",
                       "ANTHROPIC_API_KEY", use_env_ref=False)
    cfg.save_api_key("anthropic", "sk-kc")
    assert cfg.key_location("anthropic") == "keychain"
