import os

import pytest
import structlog


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("APP_"):
            monkeypatch.delenv(key)

    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()

    yield

    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
