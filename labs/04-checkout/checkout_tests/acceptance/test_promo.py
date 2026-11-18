"""Характеризационные тесты промокодов."""

from datetime import timedelta

import httpx
import pytest

from .helpers import Seed, assert_problem, checkout, checkout_body

pytestmark = pytest.mark.asyncio


async def test_percent_discount(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(price_kopecks=400_000)
    code = await seed.promo(percent_off=15)

    order = (await checkout(client, checkout_body([(sku, 1)], promo_code=code))).json()

    assert order["discount_kopecks"] == 60_000
    assert order["promo_code"] == code


async def test_discount_rounds_down_to_kopeck(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(price_kopecks=999)
    code = await seed.promo(percent_off=15)  # 149.85 копейки

    order = (await checkout(client, checkout_body([(sku, 1)], promo_code=code))).json()

    assert order["discount_kopecks"] == 149


async def test_discount_is_capped(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(price_kopecks=1_000_000)
    code = await seed.promo(percent_off=50, max_discount_kopecks=100_000)

    order = (await checkout(client, checkout_body([(sku, 1)], promo_code=code))).json()

    assert order["discount_kopecks"] == 100_000
    assert order["total_kopecks"] == 900_000


async def test_delivery_fee_is_based_on_amount_after_discount(client: httpx.AsyncClient, seed: Seed) -> None:
    # 3200 ₽ без скидки — доставка была бы бесплатной; после скидки 10% — 2880 ₽, доставка платная
    sku = await seed.product(price_kopecks=320_000)
    code = await seed.promo(percent_off=10)

    order = (await checkout(client, checkout_body([(sku, 1)], promo_code=code))).json()

    assert order["discount_kopecks"] == 32_000
    assert order["delivery_fee_kopecks"] == 29_900
    assert order["total_kopecks"] == 320_000 - 32_000 + 29_900


async def test_code_is_case_and_whitespace_insensitive(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    code = await seed.promo()

    response = await checkout(client, checkout_body([(sku, 1)], promo_code=f"  {code.lower()} "))

    assert response.status_code == 201, response.text
    assert response.json()["promo_code"] == code


async def test_min_subtotal_boundary(client: httpx.AsyncClient, seed: Seed) -> None:
    cheap = await seed.product(price_kopecks=49_999)
    exact = await seed.product(price_kopecks=50_000)
    code = await seed.promo(min_subtotal_kopecks=50_000)

    assert_problem(
        await checkout(client, checkout_body([(cheap, 1)], promo_code=code)),
        422,
        "promo-min-subtotal",
    )
    assert (await checkout(client, checkout_body([(exact, 1)], promo_code=code))).status_code == 201


async def test_unknown_code_is_422(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()

    assert_problem(
        await checkout(client, checkout_body([(sku, 1)], promo_code="NO-SUCH-PROMO")),
        422,
        "promo-not-found",
    )


async def test_expired_code_is_422(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    code = await seed.promo(valid_for=-timedelta(minutes=1))

    assert_problem(await checkout(client, checkout_body([(sku, 1)], promo_code=code)), 422, "promo-expired")


async def test_code_is_single_use_per_customer(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    code = await seed.promo()
    first = checkout_body([(sku, 1)], promo_code=code)

    assert (await checkout(client, first)).status_code == 201
    second = checkout_body([(sku, 1)], customer_id=first["customer_id"], promo_code=code)
    assert_problem(await checkout(client, second), 409, "promo-already-used")

    other_customer = checkout_body([(sku, 1)], promo_code=code)
    assert (await checkout(client, other_customer)).status_code == 201


async def test_exhausted_code_is_409(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    code = await seed.promo(max_uses=3, used_count=3)

    assert_problem(await checkout(client, checkout_body([(sku, 1)], promo_code=code)), 409, "promo-exhausted")


async def test_expired_wins_over_min_subtotal(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(price_kopecks=1_000)
    code = await seed.promo(valid_for=-timedelta(minutes=1), min_subtotal_kopecks=1_000_000)

    assert_problem(await checkout(client, checkout_body([(sku, 1)], promo_code=code)), 422, "promo-expired")


async def test_already_used_wins_over_exhausted(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    code = await seed.promo(max_uses=1)
    first = checkout_body([(sku, 1)], promo_code=code)
    assert (await checkout(client, first)).status_code == 201

    again = checkout_body([(sku, 1)], customer_id=first["customer_id"], promo_code=code)
    assert_problem(await checkout(client, again), 409, "promo-already-used")


async def test_redemption_is_recorded(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product()
    code = await seed.promo(used_count=5)

    assert (await checkout(client, checkout_body([(sku, 1)], promo_code=code))).status_code == 201

    assert await seed.promo_used_count(code) == 6
    assert await seed.redemptions(code) == 1


async def test_failed_checkout_does_not_consume_code(client: httpx.AsyncClient, seed: Seed) -> None:
    sku = await seed.product(price_kopecks=1_000)
    code = await seed.promo(min_subtotal_kopecks=1_000_000)

    await checkout(client, checkout_body([(sku, 1)], promo_code=code))

    assert await seed.promo_used_count(code) == 0
    assert await seed.redemptions(code) == 0
