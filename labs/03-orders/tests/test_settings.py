import contextlib

import pytest
from pydantic import SecretStr, ValidationError

from orders_app.settings import OrdersSettings


def test_settings(monkeypatch):
    correct_db_url = "postgresql+asyncpg://user:pwd@localhost:5555/db"
    correct_int_value_for_env = "60"
    monkeypatch.setenv("APP_DATABASE_URL", correct_db_url)
    monkeypatch.setenv("APP_DB_POOL_SIZE", correct_int_value_for_env)
    monkeypatch.setenv("APP_DB_STATEMENT_TIMEOUT_MS", correct_int_value_for_env)
    monkeypatch.setenv("APP_IDEMPOTENCY_TTL_S", correct_int_value_for_env)
    monkeypatch.setenv("APP_PAGE_DEFAULT_LIMIT", correct_int_value_for_env)

    settings = OrdersSettings(_env_file=None)
    assert isinstance(settings.database_url, SecretStr)
    assert settings.database_url.get_secret_value() == correct_db_url
    assert settings.db_pool_size == int(correct_int_value_for_env)
    assert settings.db_statement_timeout_ms == int(correct_int_value_for_env)
    assert settings.idempotency_ttl_s == int(correct_int_value_for_env)
    assert settings.page_default_limit == int(correct_int_value_for_env)


@pytest.mark.parametrize(
    ("incorrect_env_values", "expected_exception"),
    [
        pytest.param(
            [("APP_DATABASE_URL", None)],
            pytest.raises(ValidationError, match="Field required"),
            id="missing-db-url",
        ),
        pytest.param(
            [("APP_DATABASE_URL", "incorrect")],
            pytest.raises(ValueError, match="`database_url` is not a valid PostgreSQL DSN"),
            id="incorrect-db-url",
        ),
        pytest.param(
            [("APP_DB_POOL_SIZE", "0")],
            pytest.raises(ValidationError),
            id="zero-db-pool-size",
        ),
        pytest.param(
            [("APP_DB_POOL_SIZE", "-5")],
            pytest.raises(ValidationError),
            id="negative-db-pool-size",
        ),
        pytest.param(
            [("APP_DB_POOL_SIZE", "abc")],
            pytest.raises(ValidationError),
            id="missmatch-type-db-pool-size",
        ),
        pytest.param(
            [("APP_DB_STATEMENT_TIMEOUT_MS", "0")],
            pytest.raises(ValidationError),
            id="zero-db-statement-tiemout-ms",
        ),
        pytest.param(
            [("APP_DB_STATEMENT_TIMEOUT_MS", "-5")],
            pytest.raises(ValidationError),
            id="negative-db-statement-tiemout-ms",
        ),
        pytest.param(
            [("APP_DB_STATEMENT_TIMEOUT_MS", "abc")],
            pytest.raises(ValidationError),
            id="missmatch-type-db-statement-tiemout-ms",
        ),
        pytest.param(
            [("APP_IDEMPOTENCY_TTL_S", "50")],
            pytest.raises(ValidationError),
            id="low-idempotency-ttl-s",
        ),
        pytest.param(
            [("APP_PAGE_DEFAULT_LIMIT", "0")],
            pytest.raises(ValidationError),
            id="zero-page-default-limit",
        ),
        pytest.param(
            [("APP_PAGE_DEFAULT_LIMIT", "-5")],
            pytest.raises(ValidationError),
            id="negative-page-default-limit",
        ),
        pytest.param(
            [("APP_PAGE_DEFAULT_LIMIT", "101")],
            pytest.raises(ValidationError),
            id="huge-page-default-limit",
        ),
    ],
)
def test_settings_validations(
    monkeypatch,
    incorrect_env_values: list[tuple[str, str | None]],
    expected_exception,
):
    monkeypatch.setenv("APP_DATABASE_URL", "postgresql+asyncpg://user:pwd@localhost:5555/db")
    monkeypatch.setenv("APP_DB_POOL_SIZE", "10")
    monkeypatch.setenv("APP_DB_STATEMENT_TIMEOUT_MS", "2000")
    monkeypatch.setenv("APP_IDEMPOTENCY_TTL_S", "86400")
    monkeypatch.setenv("APP_PAGE_DEFAULT_LIMIT", "20")
    for env_key, env_value in incorrect_env_values:
        if env_value is None:
            monkeypatch.delenv(env_key)
        else:
            monkeypatch.setenv(env_key, env_value)

    with expected_exception:
        OrdersSettings(_env_file=None)


@pytest.mark.parametrize(
    ("db_url", "expected_db_connect_args", "expected_exception"),
    [
        pytest.param(
            "postgresql+newdriver://user:pwd@localhost:5555/db",
            None,
            pytest.raises(ValueError, match="`database_url` is not a valid PostgreSQL DSN"),
            id="incorrect-driver",
        ),
        pytest.param(
            "postgresql+asyncpg://user:pwd@localhost:5555/db",
            {"server_settings": {"statement_timeout": "2000"}},
            contextlib.nullcontext(),
            id="asyncpg-driver",
        ),
        pytest.param(
            "postgresql://user:pwd@localhost:5555/db",
            None,
            pytest.raises(ValidationError, match="must use the postgresql\\+asyncpg driver"),
            id="driver-not-specified",
        ),
        pytest.param(
            "postgresql+psycopg://user:pwd@localhost:5555/db",
            None,
            pytest.raises(ValidationError, match="must use the postgresql\\+asyncpg driver"),
            id="not-installed-driver",
        ),
    ],
)
def test_connect_args(
    monkeypatch,
    db_url: str,
    expected_db_connect_args: dict[str, dict[str, str]] | None,
    expected_exception,
):
    monkeypatch.setenv("APP_DATABASE_URL", db_url)
    monkeypatch.setenv("APP_DB_POOL_SIZE", "10")
    monkeypatch.setenv("APP_DB_STATEMENT_TIMEOUT_MS", "2000")
    monkeypatch.setenv("APP_IDEMPOTENCY_TTL_S", "86400")
    monkeypatch.setenv("APP_PAGE_DEFAULT_LIMIT", "20")

    with expected_exception:
        settings = OrdersSettings(_env_file=None)
        assert settings.db_connect_args == expected_db_connect_args
