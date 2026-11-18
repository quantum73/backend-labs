"""Характеризационные тесты отмены заказа."""

import uuid
from datetime import datetime

import httpx
import pytest

from .helpers import Seed, assert_problem, checkout, checkout_body

pytestmark = pytest.mark.asyncio


async def _cancel(client: httpx.AsyncClient, order_id: str) -> httpx.Response:
    return await client.post(f"/v1/orders/{order_id}/cancel")


async def test_cancel_restores_stock_and_marks_order(client: httpx.AsyncClient, seed: Seed) -> None:
    sku_a = await seed.product(stock=5)
    sku_b = await seed.product(stock=5)
    order = (await checkout(client, checkout_body([(sku_a, 2), (sku_b, 1)]))).json()

    response = await _cancel(client, order["id"])

    assert response.status_code == 200, response.text
    cancelled = response.json()
    assert cancelled["status"] == "cancelled"
    assert datetime.fromisoformat(cancelled["cancelled_at"]) >= datetime.fromisoformat(cancelled["created_at"])
    assert {k: v for k, v in cancelled.items() if k not in {"status", "cancelled_at"}} == {
        k: v for k, v in order.items() if k not in {"status", "cancelled_at"}
    }
    assert await seed.stock(sku_a) == 5
    assert await seed.stock(sku_b) == 5
    assert (await client.get(f"/v1/orders/{order['id']}")).json() == cancelled


async def test_cancel_releases_promo_code(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    code = await seed.promo(max_uses=1)
    body = checkout_body([(sku, 1)], promo_code=code)
    order = (await checkout(client, body)).json()

    assert (await _cancel(client, order["id"])).status_code == 200

    assert await seed.promo_used_count(code) == 0
    assert await seed.redemptions(code) == 0
    again = checkout_body([(sku, 1)], customer_id=body["customer_id"], promo_code=code)
    assert (await checkout(client, again)).status_code == 201


async def test_cancel_twice_is_409(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(stock=5)
    order = (await checkout(client, checkout_body([(sku, 1)]))).json()
    assert (await _cancel(client, order["id"])).status_code == 200

    assert_problem(await _cancel(client, order["id"]), 409, "order-not-cancellable")
    assert await seed.stock(sku) == 5


@pytest.mark.parametrize(("age_minutes", "status"), [(29, 200), (31, 409)])
async def test_cancellation_window_is_30_minutes(
    client: httpx.AsyncClient, seed: Seed, age_minutes: int, status: int
) -> None:
    sku = await seed.product(stock=5)
    order = (await checkout(client, checkout_body([(sku, 1)]))).json()
    await seed.age_order(order["id"], minutes=age_minutes)

    response = await _cancel(client, order["id"])

    assert response.status_code == status, response.text
    if status == 409:
        assert_problem(response, 409, "order-not-cancellable")
        assert await seed.stock(sku) == 4


async def test_cancel_unknown_order_is_404(client: httpx.AsyncClient) -> None:
    assert_problem(await _cancel(client, str(uuid.uuid4())), 404, "order-not-found")
