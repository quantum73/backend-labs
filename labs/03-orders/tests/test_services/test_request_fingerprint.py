import copy
import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest

from orders_app.orders.schemas import CreateOrderRequest
from orders_app.orders.services import request_fingerprint

BASE_BODY: dict[str, Any] = {
    "customer_id": str(uuid.uuid4()),
    "currency": "RUB",
    "items": [
        {"sku": "SKU-1", "quantity": 2, "unit_price_kopecks": 15_000},
        {"sku": "SKU-2", "quantity": 1, "unit_price_kopecks": 9_900},
    ],
}


def _fingerprint(raw_json: str) -> str:
    return request_fingerprint(CreateOrderRequest.model_validate_json(raw_json))


def _changed(change: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
    body = copy.deepcopy(BASE_BODY)
    change(body)
    return body


def test_fingerprint_ignores_key_order_and_formatting() -> None:
    reordered = {
        "items": [
            {"unit_price_kopecks": i["unit_price_kopecks"], "sku": i["sku"], "quantity": i["quantity"]}
            for i in BASE_BODY["items"]
        ],
        "currency": BASE_BODY["currency"],
        "customer_id": BASE_BODY["customer_id"],
    }

    compact = json.dumps(BASE_BODY, separators=(",", ":"))
    pretty = json.dumps(reordered, indent=4)

    assert list(reordered) != list(BASE_BODY)
    assert _fingerprint(compact) == _fingerprint(pretty)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param(lambda b: b.update(customer_id=str(uuid.uuid4())), id="customer-id"),
        pytest.param(lambda b: b["items"][0].update(sku="SKU-3"), id="sku"),
        pytest.param(lambda b: b["items"][0].update(quantity=3), id="quantity"),
        pytest.param(lambda b: b["items"][0].update(unit_price_kopecks=15_001), id="price"),
        # Порядок позиций — тоже значение: от него зависят line_no и порядок позиций в ответе
        pytest.param(lambda b: b["items"].reverse(), id="items-order"),
        pytest.param(
            lambda b: b["items"].append({"sku": "SKU-3", "quantity": 1, "unit_price_kopecks": 1}),
            id="extra-item",
        ),
    ],
)
def test_fingerprint_depends_on_every_value(change: Callable[[dict[str, Any]], None]) -> None:
    assert _fingerprint(json.dumps(_changed(change))) != _fingerprint(json.dumps(BASE_BODY))
