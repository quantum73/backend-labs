import traceback

import pytest
from pydantic import SecretStr, ValidationError

from foundation_app.config import LOGGED_SETTINGS_FIELDS, FoundationSettings


@pytest.mark.usefixtures("valid_env")
def test_settings_read_prefixed_env_with_types():
    settings = FoundationSettings(_env_file=None)

    assert settings.env == "test"
    assert settings.database_port == 5432
    assert settings.http_timeout_s == 14.0


def test_dump_and_repr_do_not_expose_secrets(secrets_values):
    settings = FoundationSettings(_env_file=None)

    rendered = [repr(settings), str(settings), str(settings.model_dump()), settings.model_dump_json()]

    assert not [(secret, text) for secret in secrets_values for text in rendered if secret in text]
    assert not {"database_url", "database_dsn"} & settings.model_dump().keys()


def test_database_dsn_is_secret(valid_env):
    settings = FoundationSettings(_env_file=None)
    password = valid_env["APP_DATABASE_PASSWORD"]

    dsn = settings.database_dsn()
    assert isinstance(dsn, SecretStr)
    assert dsn.get_secret_value() == f"postgresql://user:{password}@localhost:5432/dbname"
    assert password not in f"{dsn} {dsn!r}"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("APP_DATABASE_HOST", "bad host"),
        ("APP_DATABASE_HOST", ""),
        ("APP_DATABASE_DRIVER", "mysql"),
    ],
)
def test_invalid_dsn_error_hides_password(monkeypatch, valid_env, key, value):
    monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as exc_info:
        FoundationSettings(_env_file=None)

    assert valid_env["APP_DATABASE_PASSWORD"] not in "".join(traceback.format_exception(exc_info.value))


def test_database_dsn_error_has_no_context_with_password(valid_env):
    settings = FoundationSettings(_env_file=None).model_copy(update={"database_host": "bad host"})

    with pytest.raises(ValueError, match="valid PostgreSQL DSN") as exc_info:
        settings.database_dsn()

    assert exc_info.value.__context__ is None
    assert valid_env["APP_DATABASE_PASSWORD"] not in "".join(traceback.format_exception(exc_info.value))


def test_logged_fields_exist_and_are_not_secret():
    fields = FoundationSettings.model_fields
    assert set(LOGGED_SETTINGS_FIELDS) <= set(fields)
    assert not [name for name in LOGGED_SETTINGS_FIELDS if fields[name].annotation is SecretStr]
