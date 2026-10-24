import copy
import json
import threading

import pytest
import structlog

from labkit.logging import MASKED_FIELD, configure_logging, mask_sensitive

SECRET = "secret_value"  # noqa: S105


class _Unprintable:
    def __str__(self) -> str:
        raise RuntimeError("str() is broken")


def _cyclic() -> dict:
    data: dict = {}
    data["self"] = data
    return data


def _mask(fields: dict) -> dict:
    return mask_sensitive(None, "info", {"event": "probe", **fields})


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        pytest.param({"password": SECRET}, {"password": MASKED_FIELD}, id="top-level-key"),
        pytest.param({"DB_Password": SECRET}, {"DB_Password": MASKED_FIELD}, id="substring-case-insensitive"),
        pytest.param(
            {"auth": {"access_token": SECRET, "type": "bearer"}},
            {"auth": {"access_token": MASKED_FIELD, "type": "bearer"}},
            id="nested-access-token",
        ),
        pytest.param(
            {"headers": {"X-Api-Key": SECRET, "Accept": "application/json"}},
            {"headers": {"X-Api-Key": MASKED_FIELD, "Accept": "application/json"}},
            id="header-with-dashes",
        ),
        pytest.param(
            {"items": [{"secret": SECRET}, {"id": 1}]},
            {"items": [{"secret": MASKED_FIELD}, {"id": 1}]},
            id="list-of-dicts",
        ),
        pytest.param(
            {"pair": ({"token": SECRET}, "public")},
            {"pair": ({"token": MASKED_FIELD}, "public")},
            id="tuple",
        ),
        pytest.param({"card": {"number": "4444-5555"}}, {"card": MASKED_FIELD}, id="whole-subtree"),
        pytest.param({"public_id": "safe"}, {"public_id": "safe"}, id="non-sensitive"),
    ],
)
def test_mask_sensitive(fields, expected):
    assert _mask(fields) == {"event": "probe", **expected}


def test_mask_sensitive_keeps_event_name():
    assert mask_sensitive(None, "info", {"event": "token_refreshed"}) == {"event": "token_refreshed"}


def test_mask_sensitive_does_not_mutate_input():
    fields = {"auth": {"password": SECRET, "tokens": [{"token": SECRET}]}, "pair": ({"secret": SECRET},)}
    snapshot = copy.deepcopy(fields)

    _mask(fields)

    assert fields == snapshot


def test_mask_sensitive_passes_non_copyable_objects_through():
    lock = threading.Lock()

    masked = _mask({"lock": lock, "password": SECRET})

    assert masked["lock"] is lock
    assert masked["password"] == MASKED_FIELD


@pytest.mark.parametrize(
    ("broken", "error"),
    [
        pytest.param({_Unprintable(): 1}, "RuntimeError", id="key-str-raises"),
        pytest.param(_cyclic(), "RecursionError", id="cyclic-structure"),
    ],
)
def test_mask_sensitive_fails_closed(broken, error):
    masked = _mask({"password": SECRET, "user": "alice", "extra": broken})

    assert masked == {
        "event": "probe",
        "password": MASKED_FIELD,
        "user": MASKED_FIELD,
        "extra": MASKED_FIELD,
        "masking_failed": True,
        "masking_error": error,
    }


@pytest.mark.parametrize("json_output", [True, False], ids=["json", "console"])
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"password": SECRET, "headers": {"X-Api-Key": SECRET}}, id="masked"),
        pytest.param({"password": SECRET, "extra": {_Unprintable(): SECRET}}, id="masking-failed"),
    ],
)
def test_rendered_output_has_no_secret(capsys, json_output, fields):
    configure_logging(level="INFO", json=json_output)

    structlog.get_logger().info("probe", **fields)

    out, err = capsys.readouterr()
    assert "probe" in out  # событие не потеряно
    assert SECRET not in out + err


def test_contextvars_are_merged(capsys):
    configure_logging(level="INFO", json=True)
    structlog.contextvars.bind_contextvars(request_id="r-1")

    structlog.get_logger().info("probe")

    assert json.loads(capsys.readouterr().out)["request_id"] == "r-1"


def test_level_filtering(capsys):
    configure_logging(level="WARNING", json=True)
    logger = structlog.get_logger()

    logger.info("dropped")
    logger.warning("kept")

    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["event"] for line in lines] == ["kept"]
