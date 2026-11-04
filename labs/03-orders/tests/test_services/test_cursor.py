import base64
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from orders_app.core.errors import InvalidCursorError
from orders_app.core.pagination import CURSOR_VERSION, OrdersCursor, decode_cursor, encode_cursor, filters_fingerprint

CUSTOMER_ID = str(uuid.uuid4())
FILTERS_FP = filters_fingerprint({"customer_id": CUSTOMER_ID})
VALID_PAYLOAD: dict[str, Any] = {
    "v": CURSOR_VERSION,
    "t": datetime.now(UTC).isoformat(),
    "id": str(uuid.uuid4()),
    "f": FILTERS_FP,
}
MALFORMED = "Malformed cursor"
UNSUPPORTED_VERSION = "Unsupported cursor version"
OTHER_FILTERS = "Cursor was issued for different filters"


def _b64(obj: Any) -> str:
    """Курсор из произвольного JSON-значения"""
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _without(key: str) -> dict[str, Any]:
    return {k: v for k, v in VALID_PAYLOAD.items() if k != key}


@pytest.mark.parametrize(
    ("raw_cursor", "expected_detail"),
    [
        # Обёртка: не base64 / не JSON / JSON, но не объект
        pytest.param("!!!", MALFORMED, id="not-base64"),
        pytest.param("кириллица", MALFORMED, id="non-ascii"),
        pytest.param(base64.urlsafe_b64encode(b"hello").decode(), MALFORMED, id="not-json"),
        pytest.param(_b64([1, 2]), MALFORMED, id="json-list"),
        pytest.param(_b64(1), MALFORMED, id="json-number"),
        # Порча каждого поля: отсутствует или неверного типа/формата
        pytest.param(_b64(_without("v")), MALFORMED, id="missing-v"),
        pytest.param(_b64(_without("t")), MALFORMED, id="missing-t"),
        pytest.param(_b64(_without("id")), MALFORMED, id="missing-id"),
        pytest.param(_b64(_without("f")), MALFORMED, id="missing-f"),
        pytest.param(_b64({**VALID_PAYLOAD, "v": "1"}), MALFORMED, id="v-is-string"),
        pytest.param(_b64({**VALID_PAYLOAD, "t": "yesterday"}), MALFORMED, id="t-not-datetime"),
        pytest.param(_b64({**VALID_PAYLOAD, "t": "2026-01-01T00:00:00"}), MALFORMED, id="t-naive"),
        pytest.param(_b64({**VALID_PAYLOAD, "t": 5}), MALFORMED, id="t-is-int"),
        pytest.param(_b64({**VALID_PAYLOAD, "id": "nope"}), MALFORMED, id="id-not-uuid"),
        pytest.param(_b64({**VALID_PAYLOAD, "extra": 1}), MALFORMED, id="extra-field"),
        # Корректный по форме, но неприменимый
        pytest.param(_b64({**VALID_PAYLOAD, "v": CURSOR_VERSION + 1}), UNSUPPORTED_VERSION, id="unknown-version"),
        pytest.param(
            _b64({**VALID_PAYLOAD, "f": filters_fingerprint({"customer_id": str(uuid.uuid4())})}),
            OTHER_FILTERS,
            id="other-filters",
        ),
    ],
)
def test_decode_cursor_rejects_invalid_cursor(raw_cursor: str, expected_detail: str) -> None:
    with pytest.raises(InvalidCursorError) as exc:
        decode_cursor(raw_cursor, filters_fp=FILTERS_FP)
    assert exc.value.detail == expected_detail


def test_valid_payload_decodes() -> None:
    cursor = decode_cursor(_b64(VALID_PAYLOAD), filters_fp=FILTERS_FP)
    assert str(cursor.id) == VALID_PAYLOAD["id"]
    assert cursor.created_at.isoformat() == VALID_PAYLOAD["t"]


def test_roundtrip_decode_cursor() -> None:
    cursor = OrdersCursor(created_at=datetime.now(UTC), id=uuid.uuid4())
    assert decode_cursor(encode_cursor(cursor, filters_fp=FILTERS_FP), filters_fp=FILTERS_FP) == cursor
