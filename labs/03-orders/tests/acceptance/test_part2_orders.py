"""Часть 2: создание и чтение заказа, валидация входа."""

import uuid
from datetime import datetime
from typing import Any

import httpx
import pytest

from .helpers import assert_problem, create, order_body

pytestmark = pytest.mark.asyncio


async def test_create_order(client: httpx.AsyncClient) -> None:
    body = order_body()
    response = await create(client, body)

    assert response.status_code == 201, response.text
    order = response.json()
    assert response.headers["location"] == f"/v1/orders/{order['id']}"
    assert uuid.UUID(order["id"])
    assert order["customer_id"] == body["customer_id"]
    assert order["status"] == "created"
    assert order["currency"] == "RUB"
    assert order["items"] == body["items"]
    assert order["total_kopecks"] == 2 * 15_000 + 9_900  # считает сервер, а не клиент
    assert datetime.fromisoformat(order["created_at"]).tzinfo is not None


async def test_get_order_returns_same_representation(client: httpx.AsyncClient) -> None:
    created = (await create(client, order_body())).json()
    response = await client.get(f"/v1/orders/{created['id']}")
    assert response.status_code == 200
    assert response.json() == created


async def test_unknown_order_is_404_problem(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/v1/orders/{uuid.uuid4()}")
    assert_problem(response, 404, "not-found")


async def test_malformed_order_id_is_422_problem(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get("/v1/orders/not-a-uuid"), 422, "validation-error")


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"items": []}, id="no-items"),
        pytest.param({"currency": "USD"}, id="unsupported-currency"),
        pytest.param({"customer_id": "not-a-uuid"}, id="bad-customer-id"),
        pytest.param({"unexpected": True}, id="extra-field"),
        pytest.param({"items": [{"sku": "SKU-1", "quantity": 0, "unit_price_kopecks": 100}]}, id="zero-quantity"),
        pytest.param({"items": [{"sku": "SKU-1", "quantity": 1, "unit_price_kopecks": -5}]}, id="negative-price"),
        pytest.param({"items": [{"sku": "SKU-1", "quantity": 1, "unit_price_kopecks": 99.5}]}, id="float-price"),
        pytest.param({"items": [{"sku": "SKU-1", "quantity": "2", "unit_price_kopecks": 100}]}, id="string-quantity"),
        pytest.param({"items": [{"sku": "bad sku!", "quantity": 1, "unit_price_kopecks": 100}]}, id="bad-sku"),
        pytest.param(
            {"items": [{"sku": "SKU-1", "quantity": 1, "unit_price_kopecks": 100, "discount": 50}]},
            id="extra-item-field",
        ),
    ],
)
async def test_invalid_order_is_422_problem(client: httpx.AsyncClient, overrides: dict[str, Any]) -> None:
    problem = assert_problem(await create(client, order_body(**overrides)), 422, "validation-error")
    assert problem["errors"], "ожидается список ошибок валидации"
    assert all({"loc", "msg", "type"} <= set(error) for error in problem["errors"])


async def test_validation_problem_does_not_echo_input(client: httpx.AsyncClient) -> None:
    card_like = "4111111111111111"
    response = await create(client, order_body(card_number=card_like))
    assert_problem(response, 422, "validation-error")
    assert card_like not in response.text


async def test_malformed_json_is_422_problem(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/orders",
        content=b'{"customer_id": ',
        headers={"Idempotency-Key": uuid.uuid4().hex, "Content-Type": "application/json"},
    )
    assert_problem(response, 422, "validation-error")
