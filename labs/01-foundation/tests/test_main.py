import json

import pytest
import structlog

from foundation_app.config import get_settings
from foundation_app.main import main as main_func


@pytest.fixture(autouse=True)
def clear_get_settings_cache():
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def clean_structlog_context():
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def test_main_correct_settings(monkeypatch, capsys):
    env_value = "test"
    db_url = "postgresql://user:fake_password@localhost:5432/dbname"
    test_api_token = "fake_secret_token_12345"  # noqa: S105
    expected_masked_api_token_value = "**********"  # noqa: S105
    http_timeout = 14.0
    monkeypatch.setenv("APP_ENV", env_value)
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("APP_API_TOKEN", test_api_token)
    monkeypatch.setenv("APP_HTTP_TIMEOUT_S", str(http_timeout))

    with pytest.raises(SystemExit) as exc_info:
        main_func()

    assert exc_info.value.code == 0

    captured = capsys.readouterr()
    err_lines = captured.err.strip().split("\n")
    assert len(err_lines) == 1
    assert not err_lines[0]

    log_lines = captured.out.strip().split("\n")
    assert len(log_lines) == 1

    log_entry = json.loads(log_lines[0])
    assert log_entry["event"] == "app_started"
    assert log_entry["level"] == "info"
    assert log_entry["run_id"]
    assert log_entry["settings"]["log_level"] == "INFO"
    assert log_entry["settings"]["log_json"]
    assert log_entry["settings"]["env"] == env_value
    assert log_entry["settings"]["database_url"] == db_url
    assert log_entry["settings"]["api_token"] == expected_masked_api_token_value
    assert log_entry["settings"]["http_timeout_s"] == http_timeout
    assert "super_secret_token_123" not in captured.out


def test_main_incorrect_settings(monkeypatch, capsys):
    env_value = "test"
    db_url = "postgresql://user:fake_password@localhost:5432/dbname"
    test_api_token = "fake_secret_token_12345"  # noqa: S105
    invalid_http_timeout = 140.0
    monkeypatch.setenv("APP_ENV", env_value)
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("APP_API_TOKEN", test_api_token)
    monkeypatch.setenv("APP_HTTP_TIMEOUT_S", str(invalid_http_timeout))

    with pytest.raises(SystemExit) as exc_info:
        main_func()

    assert exc_info.value.code == 2

    captured = capsys.readouterr()
    err_lines = captured.err.strip().split("\n")
    assert len(err_lines) == 1
    assert not err_lines[0]

    log_lines = captured.out.strip().split("\n")
    assert len(log_lines) == 1

    log_entry = json.loads(log_lines[0])
    assert log_entry["event"] == "config_invalid"
    assert log_entry["level"] == "error"
    assert "run_id" not in log_entry
    assert len(log_entry["errors"]) == 1
    assert log_entry["errors"][0]["http_timeout_s"] == "Input should be less than or equal to 30"
