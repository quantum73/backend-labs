import json
import os

import pytest
import structlog

from foundation_app.config import get_settings
from foundation_app.main import main as main_func


@pytest.fixture(autouse=True)
def cleaner(monkeypatch):
    get_settings.cache_clear()
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    # Removing all environment keys
    for key in os.environ:
        if key.startswith("APP_"):
            monkeypatch.delenv(key, raising=False)

    yield

    structlog.contextvars.clear_contextvars()


def test_main_correct_settings(monkeypatch, capsys):
    env_value = "test"
    db_driver = "postgresql"
    db_user = "user"
    db_password = "fake_password"  # noqa: S105
    db_host = "localhost"
    db_port = 5432
    db_name = "dbname"
    test_api_token = "fake_secret_token_12345"  # noqa: S105
    expected_masked_api_token_value = "***"  # noqa: S105
    http_timeout = 14.0
    monkeypatch.setenv("APP_ENV", env_value)
    monkeypatch.setenv("APP_DATABASE_DRIVER", db_driver)
    monkeypatch.setenv("APP_DATABASE_USER", db_user)
    monkeypatch.setenv("APP_DATABASE_PASSWORD", db_password)
    monkeypatch.setenv("APP_DATABASE_HOST", db_host)
    monkeypatch.setenv("APP_DATABASE_PORT", str(db_port))
    monkeypatch.setenv("APP_DATABASE_NAME", db_name)
    monkeypatch.setenv("APP_API_TOKEN", test_api_token)
    monkeypatch.setenv("APP_HTTP_TIMEOUT_S", str(http_timeout))

    assert main_func() == 0

    captured = capsys.readouterr()
    err_lines = captured.err.strip()
    assert test_api_token not in err_lines
    err_lines = err_lines.split("\n")
    assert len(err_lines) == 1
    assert not err_lines[0]

    log_lines = captured.out.strip()
    assert test_api_token not in log_lines
    log_lines = log_lines.split("\n")
    assert len(log_lines) == 1

    log_entry = json.loads(log_lines[0])
    assert log_entry["event"] == "app_started"
    assert log_entry["level"] == "info"
    assert log_entry["run_id"]
    assert log_entry["settings"]["log_json"]
    assert log_entry["settings"]["env"] == env_value
    assert "database_url" not in log_entry["settings"]
    assert "database_password" not in log_entry["settings"]
    assert log_entry["settings"]["api_token"] == expected_masked_api_token_value
    assert log_entry["settings"]["http_timeout_s"] == http_timeout


def test_main_incorrect_settings(monkeypatch, capsys):
    env_value = "test"
    db_driver = "postgresql"
    db_user = "user"
    db_password = "fake_password"  # noqa: S105
    db_host = "localhost"
    db_port = 5432
    db_name = "dbname"
    test_api_token = "fake_secret_token_12345"  # noqa: S105
    invalid_http_timeout = 140.0
    monkeypatch.setenv("APP_ENV", env_value)
    monkeypatch.setenv("APP_DATABASE_DRIVER", db_driver)
    monkeypatch.setenv("APP_DATABASE_USER", db_user)
    monkeypatch.setenv("APP_DATABASE_PASSWORD", db_password)
    monkeypatch.setenv("APP_DATABASE_HOST", db_host)
    monkeypatch.setenv("APP_DATABASE_PORT", str(db_port))
    monkeypatch.setenv("APP_DATABASE_NAME", db_name)
    monkeypatch.setenv("APP_API_TOKEN", test_api_token)
    monkeypatch.setenv("APP_HTTP_TIMEOUT_S", str(invalid_http_timeout))

    assert main_func() == 2

    captured = capsys.readouterr()
    err_lines = captured.err.strip()
    assert test_api_token not in err_lines
    err_lines = err_lines.split("\n")
    assert len(err_lines) == 1
    assert not err_lines[0]

    log_lines = captured.out.strip()
    assert test_api_token not in log_lines
    log_lines = log_lines.split("\n")
    assert len(log_lines) == 1

    log_entry = json.loads(log_lines[0])
    assert log_entry["event"] == "config_invalid"
    assert log_entry["level"] == "error"
    assert "run_id" not in log_entry
    assert len(log_entry["errors"]) == 1
    assert log_entry["errors"][0]["field"] == "http_timeout_s"
    assert log_entry["errors"][0]["msg"] == "Input should be less than or equal to 30"
