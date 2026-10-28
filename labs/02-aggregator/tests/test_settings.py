import re

import pytest
from pydantic import ValidationError

from aggregator_app.client import UpstreamConfig
from aggregator_app.pipeline import PipelineConfig
from aggregator_app.settings import MAX_GRACE_PERIOD_S, AggregatorSettings


def _settings() -> AggregatorSettings:
    return AggregatorSettings(_env_file=None)  # pyright: ignore[reportCallIssue]


def test_defaults():
    settings = _settings()

    assert (settings.deadline_s, settings.workers, settings.queue_size, settings.grace_period_s) == (1.0, 8, 100, 5.0)
    assert (settings.max_concurrency, settings.per_source_concurrency, settings.max_attempts) == (20, 5, 3)
    assert settings.verify_workers is None


def test_env_overrides(monkeypatch):
    monkeypatch.setenv("APP_WORKERS", "16")
    monkeypatch.setenv("APP_QUEUE_SIZE", "7")
    monkeypatch.setenv("APP_DEADLINE_S", "0.5")
    monkeypatch.setenv("APP_VERIFY_WORKERS", "2")

    settings = _settings()

    assert (settings.workers, settings.queue_size, settings.deadline_s, settings.verify_workers) == (16, 7, 0.5, 2)


@pytest.mark.parametrize(
    ("env", "field"),
    [
        pytest.param({"APP_WORKERS": "0"}, "workers", id="no-workers"),
        pytest.param({"APP_QUEUE_SIZE": "0"}, "queue_size", id="unbounded-queue"),
        pytest.param({"APP_DEADLINE_S": "0"}, "deadline_s", id="zero-deadline"),
        pytest.param({"APP_GRACE_PERIOD_S": str(MAX_GRACE_PERIOD_S + 1)}, "grace_period_s", id="grace-too-long"),
        pytest.param({"APP_VERIFY_WORKERS": "0"}, "verify_workers", id="no-verify-workers"),
    ],
)
def test_field_validation(monkeypatch, env: dict[str, str], field: str):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError) as exc_info:
        _settings()

    assert [error["loc"] for error in exc_info.value.errors()] == [(field,)]


@pytest.mark.parametrize(
    ("env", "message"),
    [
        pytest.param(
            {"APP_PER_SOURCE_CONCURRENCY": "21"},
            "`per_source_concurrency` has to be <= `max_concurrency`",
            id="per-source-above-global",
        ),
        pytest.param(
            {"APP_BACKOFF_BASE_S": "1.0", "APP_BACKOFF_CAP_S": "0.5"},
            "`backoff_cap_s` has to be >= `backoff_base_s`",
            id="cap-below-base",
        ),
        pytest.param(
            {"APP_GRACE_PERIOD_S": "1.0"},
            "`grace_period_s` has to be >= `deadline_s` + `watchdog_margin_s`",
            id="grace-shorter-than-sku",
        ),
    ],
)
def test_cross_field_validation(monkeypatch, env: dict[str, str], message: str):
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    with pytest.raises(ValidationError, match=re.escape(message)):
        _settings()


def test_validation_error_hides_input(monkeypatch):
    monkeypatch.setenv("APP_WORKERS", "not-a-number-42")

    with pytest.raises(ValidationError) as exc_info:
        _settings()

    assert "not-a-number-42" not in str(exc_info.value)


def test_to_upstream_config(monkeypatch):
    monkeypatch.setenv("APP_MAX_CONCURRENCY", "10")
    monkeypatch.setenv("APP_PER_SOURCE_CONCURRENCY", "2")
    monkeypatch.setenv("APP_MAX_ATTEMPTS", "4")
    monkeypatch.setenv("APP_BACKOFF_BASE_S", "0.1")
    monkeypatch.setenv("APP_BACKOFF_CAP_S", "0.2")
    monkeypatch.setenv("APP_VERIFY_WORKERS", "3")

    config = _settings().to_upstream_config()

    assert config == UpstreamConfig(
        max_concurrency=10,
        per_source_concurrency=2,
        max_attempts=4,
        backoff_base_s=0.1,
        backoff_cap_s=0.2,
        verify_workers=3,
    )


def test_to_upstream_config_keeps_default_verify_workers():
    assert _settings().to_upstream_config().verify_workers == UpstreamConfig().verify_workers


def test_to_pipeline_config_takes_sources_from_run_input():
    config = _settings().to_pipeline_config(["alpha", "beta"])

    assert config == PipelineConfig(
        sources=("alpha", "beta"),
        workers=8,
        queue_size=100,
        deadline_s=1.0,
        grace_period_s=5.0,
        watchdog_margin_s=0.1,
    )
