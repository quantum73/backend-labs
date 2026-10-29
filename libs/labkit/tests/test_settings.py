import pytest
from pydantic import BaseModel, Field, ValidationError, model_validator

from labkit.settings import AppSettings, summarize_validation_errors


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


class _Nested(BaseModel):
    port: int


class _Config(BaseModel):
    token: str = Field(min_length=10)
    nested: _Nested

    @model_validator(mode="after")
    def _check(self) -> _Config:
        raise ValueError("cross-field rule failed")


def test_summarize_validation_errors_hides_input_and_flattens_loc():
    with pytest.raises(ValidationError) as exc_info:
        _Config.model_validate({"token": "short-sec", "nested": {"port": "not-a-port"}})

    summary = summarize_validation_errors(exc_info.value)

    assert summary == [
        {"field": "token", "type": "string_too_short", "msg": "String should have at least 10 characters"},
        {"field": "nested.port", "type": "int_parsing", "msg": summary[1]["msg"]},
    ]
    assert "short-sec" not in str(summary)
    assert "not-a-port" not in str(summary)


def test_summarize_validation_errors_uses_root_for_model_level_errors():
    with pytest.raises(ValidationError) as exc_info:
        _Config.model_validate({"token": "long-enough-token", "nested": {"port": 1}})

    assert summarize_validation_errors(exc_info.value) == [
        {"field": "root", "type": "value_error", "msg": "Value error, cross-field rule failed"},
    ]
