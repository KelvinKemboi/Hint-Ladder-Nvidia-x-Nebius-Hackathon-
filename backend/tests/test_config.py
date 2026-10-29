from sparring_partner.config import Settings


def test_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "secret")
    monkeypatch.setenv("MODEL_FAST", "fast-model")

    settings = Settings(_env_file=None)

    assert settings.nebius_api_key.get_secret_value() == "secret"
    assert settings.model_fast == "fast-model"


def test_api_key_not_exposed_in_repr(monkeypatch):
    monkeypatch.setenv("NEBIUS_API_KEY", "secret")

    assert "secret" not in repr(Settings(_env_file=None))
