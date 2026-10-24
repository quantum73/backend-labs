import os

import pytest
import structlog
from structlog.testing import LogCapture

from labkit.logging import configure_logging


@pytest.fixture(name="log_output")
def fixture_log_output():
    return LogCapture()


@pytest.fixture(autouse=True)
def setup_logging(log_output, monkeypatch):
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
    # Removing all environment keys
    for key in os.environ:
        if key.startswith("APP_"):
            monkeypatch.delenv(key, raising=False)

    configure_logging(level="INFO", json=False)
    current_processors = list(structlog.get_config()["processors"])
    current_processors.insert(-1, log_output)
    structlog.configure(processors=current_processors)


def test_mask_sensitive_fields_and_no_mutation(log_output):
    logger = structlog.get_logger()
    expected_masked_value = "***"

    source_payload = {
        "Authorization": [
            {"value": "secret_value"},
            {"another_value": "secret_value"},
        ],
        "auth": {
            "Password": "secret_password_123",
            "tokens_list": [
                {"token": "secret_token", "type": "bearer"},
                {"api_key": "secret_api_key", "secret": "secret_payload"},
            ],
        },
        "public_id": "safe_id",
        "card": {"number": "4444-5555"},
    }

    logger.info("test_event", **source_payload)
    assert len(log_output.entries) == 1

    logged_event = log_output.entries[0]
    assert logged_event["Authorization"] == expected_masked_value
    assert logged_event["auth"]["Password"] == expected_masked_value
    assert logged_event["auth"]["tokens_list"] == expected_masked_value
    assert logged_event["public_id"] == "safe_id"
    assert logged_event["card"] == expected_masked_value

    # Проверяем, что исходный объект не мутировал
    assert source_payload["Authorization"][0]["value"] == "secret_value"
    assert source_payload["Authorization"][1]["another_value"] == "secret_value"
    assert source_payload["auth"]["Password"] == "secret_password_123"  # noqa: S105
    assert source_payload["auth"]["tokens_list"][0]["token"] == "secret_token"  # noqa: S105
    assert source_payload["auth"]["tokens_list"][1]["api_key"] == "secret_api_key"
    assert source_payload["auth"]["tokens_list"][1]["secret"] == "secret_payload"  # noqa: S105
    assert source_payload["card"]["number"] == "4444-5555"
