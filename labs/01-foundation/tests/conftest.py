import os

import pytest
import structlog

from foundation_app import main as main_module
from foundation_app.config import FoundationSettings


def _settings_without_dotenv() -> FoundationSettings:
    return FoundationSettings(_env_file=None)


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("APP_"):
            monkeypatch.delenv(key)

    monkeypatch.setattr(main_module, "get_settings", _settings_without_dotenv)
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()

    yield

    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
def valid_env(monkeypatch) -> dict[str, str]:
    env = {
        "APP_ENV": "test",
        "APP_DATABASE_DRIVER": "postgresql",
        "APP_DATABASE_USER": "user",
        "APP_DATABASE_PASSWORD": "Pg_P4ssw0rd",
        "APP_DATABASE_HOST": "localhost",
        "APP_DATABASE_PORT": "5432",
        "APP_DATABASE_NAME": "dbname",
        "APP_API_TOKEN": "fake_secret_token_12345",
        "APP_HTTP_TIMEOUT_S": "14.0",
    }
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


@pytest.fixture
def secrets(valid_env) -> tuple[str, ...]:
    return valid_env["APP_DATABASE_PASSWORD"], valid_env["APP_API_TOKEN"]
