import uuid
from typing import Any

import httpx

PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "https://errors.orders.example/"


def new_key() -> str:
    return uuid.uuid4().hex


def order_body(customer_id: str | None = None, **overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "customer_id": customer_id or str(uuid.uuid4()),
        "currency": "RUB",
        "items": [
            {"sku": "SKU-1", "quantity": 2, "unit_price_kopecks": 15_000},
            {"sku": "SKU-2", "quantity": 1, "unit_price_kopecks": 9_900},
        ],
    }
    body.update(overrides)
    return body


async def create(client: httpx.AsyncClient, body: dict[str, Any], key: str | None = None) -> httpx.Response:
    return await client.post("/v1/orders", json=body, headers={"Idempotency-Key": key or new_key()})


def assert_problem(response: httpx.Response, status: int, slug: str | None = None) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON), response.headers["content-type"]
    problem: dict[str, Any] = response.json()
    assert problem["status"] == status
    assert problem["title"]
    assert problem["type"].startswith(TYPE_BASE)
    if slug is not None:
        assert problem["type"] == TYPE_BASE + slug, problem
    return problem
