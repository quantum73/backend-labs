"""
40 параллельных POST /v1/orders с одним Idempotency-Key через настоящий HTTP.
Запуск при поднятом сервере (несколько воркеров): uv run python labs/03-orders/scripts/concurrency_check.py
Код выхода 1, если результат не совпал с ожидаемым: 201 у всех, один id, 39 повторов, один заказ у клиента.
"""

import asyncio
import os
import sys
import uuid
from asyncio import TaskGroup
from dataclasses import dataclass
from typing import Any

import httpx

REQUESTS_COUNT = 40
CUSTOMER_ID = str(uuid.uuid4())
IDEMPOTENCY_KEY = str(uuid.uuid4())
BASE_API_URL = os.environ.get("ORDERS_BASE_URL", "http://127.0.0.1:8000")


@dataclass(frozen=True, slots=True)
class APIResult:
    status_code: int
    order_id: str
    replay: bool
    payload: dict[str, Any]


async def create_order(client: httpx.AsyncClient) -> APIResult:
    body: dict[str, Any] = {
        "customer_id": CUSTOMER_ID,
        "currency": "RUB",
        "items": [
            {"sku": "SKU-1", "quantity": 2, "unit_price_kopecks": 1000},
            {"sku": "SKU-2", "quantity": 3, "unit_price_kopecks": 500},
        ],
    }
    response = await client.post(
        f"{BASE_API_URL}/v1/orders",
        json=body,
        headers={"Idempotency-Key": IDEMPOTENCY_KEY},
    )
    replayed_header = response.headers.get("Idempotent-Replayed", "false")
    data = response.json()
    return APIResult(
        status_code=response.status_code,
        order_id=data.get("id", None),
        replay=replayed_header == "true",
        payload=data,
    )


async def main() -> None:
    async with httpx.AsyncClient() as client, TaskGroup() as tg:
        tasks: list[asyncio.Task] = [tg.create_task(create_order(client)) for _ in range(REQUESTS_COUNT)]

    statuses = set()
    ids = set()
    replayed = 0
    for t in tasks:
        res: APIResult = t.result()
        statuses.add(res.status_code)
        ids.add(res.order_id)
        replayed += int(res.replay)

    print(f"statuses {statuses} ids {len(ids)} replayed {replayed}")

    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{BASE_API_URL}/v1/orders",
            params={"customer_id": CUSTOMER_ID, "limit": 100},
        )
        response.raise_for_status()
        orders_in_db = len(response.json()["items"])
        print(f"orders in db: {orders_in_db}")

    ok = statuses == {201} and len(ids) == 1 and replayed == REQUESTS_COUNT - 1 and orders_in_db == 1
    print("OK" if ok else "FAILED: expected statuses {201}, ids 1, replayed 39, orders in db 1")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
