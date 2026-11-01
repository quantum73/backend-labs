"""Часть 3: идемпотентность POST /v1/orders через заголовок Idempotency-Key."""

import asyncio
import json

import httpx
import pytest

from .helpers import assert_problem, create, new_key, order_body

pytestmark = pytest.mark.asyncio


async def _orders_of(client: httpx.AsyncClient, customer_id: str) -> list[dict[str, object]]:
    response = await client.get("/v1/orders", params={"customer_id": customer_id, "limit": 100})
    assert response.status_code == 200, response.text
    items: list[dict[str, object]] = response.json()["items"]
    return items


@pytest.mark.parametrize("key", [None, "short", "has space inside", "x" * 65])
async def test_missing_or_invalid_key_is_400(client: httpx.AsyncClient, key: str | None) -> None:
    headers = {} if key is None else {"Idempotency-Key": key}
    response = await client.post("/v1/orders", json=order_body(), headers=headers)
    assert_problem(response, 400, "idempotency-key-required")


async def test_replay_returns_stored_response(client: httpx.AsyncClient) -> None:
    body, key = order_body(), new_key()
    first = await create(client, body, key)
    second = await create(client, body, key)

    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["location"] == first.headers["location"]
    assert "idempotent-replayed" not in first.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert len(await _orders_of(client, body["customer_id"])) == 1


async def test_same_payload_with_different_formatting_is_a_replay(client: httpx.AsyncClient) -> None:
    body, key = order_body(), new_key()
    first = await create(client, body, key)
    reordered = json.dumps({"items": body["items"], "currency": "RUB", "customer_id": body["customer_id"]}, indent=4)
    second = await client.post(
        "/v1/orders", content=reordered, headers={"Idempotency-Key": key, "Content-Type": "application/json"}
    )
    assert second.status_code == 201
    assert second.json()["id"] == first.json()["id"]


async def test_key_reuse_with_different_payload_is_422(client: httpx.AsyncClient) -> None:
    key = new_key()
    assert (await create(client, order_body(), key)).status_code == 201
    response = await create(client, order_body(), key)  # другой customer_id
    assert_problem(response, 422, "idempotency-key-reuse")


async def test_different_keys_create_different_orders(client: httpx.AsyncClient) -> None:
    body = order_body()
    first, second = await create(client, body), await create(client, body)
    assert first.json()["id"] != second.json()["id"]
    assert len(await _orders_of(client, body["customer_id"])) == 2


async def test_concurrent_duplicates_create_exactly_one_order(client: httpx.AsyncClient) -> None:
    body, key = order_body(), new_key()
    responses = await asyncio.gather(*(create(client, body, key) for _ in range(10)))

    assert {r.status_code for r in responses} == {201}, [r.text for r in responses]
    assert len({r.json()["id"] for r in responses}) == 1
    assert sum(r.headers.get("idempotent-replayed") == "true" for r in responses) == 9
    assert len(await _orders_of(client, body["customer_id"])) == 1


async def test_failed_validation_does_not_burn_the_key(client: httpx.AsyncClient) -> None:
    key = new_key()
    assert_problem(await create(client, order_body(items=[]), key), 422, "validation-error")
    assert (await create(client, order_body(), key)).status_code == 201
