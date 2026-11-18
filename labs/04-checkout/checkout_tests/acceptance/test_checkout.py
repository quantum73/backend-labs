"""Характеризационные тесты оформления заказа: фиксируют, как сервис ведёт себя сейчас."""

import re
import uuid
from datetime import datetime

import httpx
import pytest

from .helpers import FakeNotifier, Seed, assert_problem, checkout, checkout_body

pytestmark = pytest.mark.asyncio

# Формат дат в ответе — часть контракта: так их отдаёт legacy (datetime.isoformat)
LEGACY_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{6})?\+00:00$")


async def test_checkout_creates_order(client: httpx.AsyncClient, seed: Seed) -> None:
    sku_a = await seed.product(price_kopecks=150_000)
    sku_b = await seed.product(price_kopecks=99_900)
    body = checkout_body([(sku_a, 2), (sku_b, 1)])

    response = await checkout(client, body)

    assert response.status_code == 201, response.text
    order = response.json()
    assert response.headers["location"] == f"/v1/orders/{order['id']}"
    assert uuid.UUID(order["id"])
    assert order["customer_id"] == body["customer_id"]
    assert order["email"] == body["email"]
    assert order["status"] == "created"
    assert order["lines"] == [
        {"sku": sku_a, "quantity": 2, "unit_price_kopecks": 150_000, "line_total_kopecks": 300_000},
        {"sku": sku_b, "quantity": 1, "unit_price_kopecks": 99_900, "line_total_kopecks": 99_900},
    ]
    assert order["subtotal_kopecks"] == 399_900
    assert order["discount_kopecks"] == 0
    assert order["delivery_fee_kopecks"] == 0
    assert order["total_kopecks"] == 399_900
    assert order["promo_code"] is None
    assert order["cancelled_at"] is None


async def test_timestamps_keep_legacy_format(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    order = (await checkout(client, checkout_body([(sku, 1)]))).json()

    assert LEGACY_TIMESTAMP.match(order["created_at"]), order["created_at"]
    assert datetime.fromisoformat(order["created_at"]).tzinfo is not None


async def test_get_order_returns_the_same_representation(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    created = (await checkout(client, checkout_body([(sku, 3)]))).json()

    response = await client.get(f"/v1/orders/{created['id']}")

    assert response.status_code == 200
    assert response.json() == created


async def test_unknown_order_is_404(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get(f"/v1/orders/{uuid.uuid4()}"), 404, "order-not-found")


@pytest.mark.parametrize(
    ("price_kopecks", "quantity", "delivery_fee"),
    [
        pytest.param(299_999, 1, 29_900, id="just-below-threshold"),
        pytest.param(300_000, 1, 0, id="exactly-threshold-is-free"),
        pytest.param(100_000, 3, 0, id="threshold-reached-by-quantity"),
        pytest.param(1_000, 1, 29_900, id="small-order"),
    ],
)
async def test_delivery_fee_threshold(
    client: httpx.AsyncClient, seed: Seed, price_kopecks: int, quantity: int, delivery_fee: int
) -> None:
    sku = await seed.product(price_kopecks=price_kopecks)

    order = (await checkout(client, checkout_body([(sku, quantity)]))).json()

    assert order["delivery_fee_kopecks"] == delivery_fee
    assert order["total_kopecks"] == price_kopecks * quantity + delivery_fee


async def test_duplicate_skus_are_merged_in_order_of_first_appearance(client: httpx.AsyncClient, seed: Seed) -> None:
    sku_a = await seed.product(price_kopecks=1_000)
    sku_b = await seed.product(price_kopecks=2_000)

    order = (await checkout(client, checkout_body([(sku_a, 1), (sku_b, 2), (sku_a, 3)]))).json()

    assert [(line["sku"], line["quantity"]) for line in order["lines"]] == [(sku_a, 4), (sku_b, 2)]


async def test_merged_quantity_above_limit_is_422(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(stock=100)

    problem = assert_problem(
        await checkout(client, checkout_body([(sku, 6), (sku, 5)])),
        422,
        "quantity-limit",
    )
    assert sku in problem["detail"]
    assert await seed.stock(sku) == 100


async def test_checkout_decrements_stock(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(stock=5)

    assert (await checkout(client, checkout_body([(sku, 2)]))).status_code == 201

    assert await seed.stock(sku) == 3


async def test_insufficient_stock_is_409_and_changes_nothing(client: httpx.AsyncClient, seed: Seed) -> None:
    plenty = await seed.product(stock=10)
    scarce = await seed.product(stock=1)
    body = checkout_body([(plenty, 2), (scarce, 2)])

    problem = assert_problem(await checkout(client, body), 409, "insufficient-stock")

    assert scarce in problem["detail"]
    assert await seed.stock(plenty) == 10
    assert await seed.stock(scarce) == 1
    assert await seed.orders_of(body["customer_id"]) == 0


async def test_unknown_sku_is_422(client: httpx.AsyncClient, seed: Seed) -> None:
    known = await seed.product()

    problem = assert_problem(
        await checkout(client, checkout_body([(known, 1), ("SKU-DOES-NOT-EXIST", 1)])),
        422,
        "unknown-sku",
    )
    assert "SKU-DOES-NOT-EXIST" in problem["detail"]


async def test_unknown_sku_wins_over_insufficient_stock_and_bad_promo(client: httpx.AsyncClient, seed: Seed) -> None:
    # Порядок проверок — тоже поведение: клиент видит первую ошибку
    scarce = await seed.product(stock=0)
    body = checkout_body([(scarce, 1), ("SKU-DOES-NOT-EXIST", 1)], promo_code="NO-SUCH-PROMO")

    assert_problem(await checkout(client, body), 422, "unknown-sku")


async def test_insufficient_stock_wins_over_bad_promo(client: httpx.AsyncClient, seed: Seed) -> None:
    scarce = await seed.product(stock=0)

    assert_problem(
        await checkout(client, checkout_body([(scarce, 1)], promo_code="NO-SUCH-PROMO")),
        409,
        "insufficient-stock",
    )


@pytest.mark.parametrize(
    "body_patch",
    [
        pytest.param({"items": []}, id="no-items"),
        pytest.param({"items": [{"sku": "SKU-1", "quantity": 0}]}, id="zero-quantity"),
        pytest.param({"items": [{"sku": "SKU-1", "quantity": 11}]}, id="quantity-above-10"),
        pytest.param({"items": [{"sku": f"SKU-{i}", "quantity": 1} for i in range(21)]}, id="too-many-lines"),
        pytest.param({"email": "not-an-email"}, id="bad-email"),
        pytest.param({"customer_id": "42"}, id="bad-customer-id"),
        pytest.param({"unexpected": True}, id="extra-field"),
    ],
)
async def test_invalid_request_is_422(client: httpx.AsyncClient, body_patch: dict[str, object]) -> None:
    body = checkout_body([("SKU-1", 1)]) | body_patch

    problem = assert_problem(await checkout(client, body), 422, "validation-error")

    assert problem["errors"]
    assert all({"loc", "msg", "type"} == set(error) for error in problem["errors"])


async def test_notification_is_sent_once_with_order_data(
    client: httpx.AsyncClient, seed: Seed, notifier: FakeNotifier
) -> None:
    sku = await seed.product(price_kopecks=10_000)
    body = checkout_body([(sku, 1)], email="anna@example.com")

    order = (await checkout(client, body)).json()

    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert (call["method"], call["path"]) == ("POST", "/v1/notifications")
    assert call["payload"] == {
        "template": "order_created",
        "email": "anna@example.com",
        "order_id": order["id"],
        "total_kopecks": order["total_kopecks"],
    }


async def test_failed_checkout_sends_no_notification(
    client: httpx.AsyncClient, seed: Seed, notifier: FakeNotifier
) -> None:
    sku = await seed.product(stock=0)

    await checkout(client, checkout_body([(sku, 1)]))

    assert notifier.calls == []
