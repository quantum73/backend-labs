import pytest
from pydantic import ValidationError

from labkit.settings import AppSettings


def test_base_settings_need_no_app_specific_fields():
    settings = AppSettings(_env_file=None)
    assert settings.model_dump() == {"env": "local", "log_level": "INFO", "log_json": True}


def test_base_settings_read_prefixed_env(monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("APP_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("APP_LOG_JSON", "false")

    settings = AppSettings(_env_file=None)
    assert (settings.env, settings.log_level, settings.log_json) == ("staging", "DEBUG", False)


def test_validation_error_hides_input(monkeypatch):
    monkeypatch.setenv("APP_LOG_LEVEL", "secret_value")

    with pytest.raises(ValidationError) as exc_info:
        AppSettings(_env_file=None)

    assert "secret_value" not in str(exc_info.value)
