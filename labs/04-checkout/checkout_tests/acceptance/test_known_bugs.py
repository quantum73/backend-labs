"""
Известные проблемы legacy, зафиксированные тестами.

Это не желаемое поведение, а текущее. Пока идёт рефакторинг (части 1–3), оно должно сохраняться:
рефакторинг не меняет поведение. В части 4 этот файл удаляется, а новое поведение фиксирует test_part4_behavior.py.
"""

import httpx
import pytest

from .helpers import FakeNotifier, Seed, checkout, checkout_body

pytestmark = pytest.mark.asyncio


async def test_notifier_failure_fails_the_whole_checkout(
    client: httpx.AsyncClient, seed: Seed, notifier: FakeNotifier
) -> None:
    sku = await seed.product(stock=5)
    notifier.mode = "fail"
    body = checkout_body([(sku, 1)])

    response = await checkout(client, body)

    assert response.status_code == 500
    assert await seed.orders_of(body["customer_id"]) == 0
    assert await seed.stock(sku) == 5
    assert len(notifier.calls) == 1  # письмо при этом могло уйти: клиент получил его, а заказа нет


async def test_notification_is_sent_before_commit(
    client: httpx.AsyncClient, seed: Seed, notifier: FakeNotifier
) -> None:
    sku = await seed.product()

    assert (await checkout(client, checkout_body([(sku, 1)]))).status_code == 201

    [call] = notifier.calls
    assert call["order_committed"] is False
