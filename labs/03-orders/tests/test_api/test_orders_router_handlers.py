import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from fastapi import Request
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection

from orders_app.db import get_db_conn
from orders_app.settings import OrdersSettings
from tests.acceptance.helpers import assert_problem, create, order_body
from tests.helpers import db_queries_count

pytestmark = pytest.mark.asyncio


def _read_log_events(capsys) -> list[dict]:
    out, err = capsys.readouterr()
    assert not err
    lines = out.splitlines()
    return [json.loads(line) for line in lines]


def _items(*skus: str, quantity: int = 1, price: int = 100) -> list[dict[str, Any]]:
    return [{"sku": sku, "quantity": quantity, "unit_price_kopecks": price} for sku in skus]


async def _paginated_orders_request(client: httpx.AsyncClient, customer_id: str, **params: Any) -> httpx.Response:
    return await client.get("/v1/orders", params={"customer_id": customer_id, **params})


async def test_get_order_by_id(client: httpx.AsyncClient, capsys) -> None:
    created_response_data = (await client.post("/v1/orders", json=order_body())).json()
    response = await client.get(f"/v1/orders/{created_response_data['id']}")
    assert response.status_code == 200
    assert response.json() == created_response_data

    get_order_log_event = _read_log_events(capsys)[-1]
    assert get_order_log_event["route"] == "/v1/orders/{order_id}"
    assert get_order_log_event["status"] == 200


@pytest.mark.parametrize(
    "sku",
    ["lower-1", "A.B", "_X", "-A"],
    ids=["lowercase", "dot", "underscore", "leading-dash"],
)
async def test_create_order_with_bad_sku(client: httpx.AsyncClient, sku: str) -> None:
    problem = assert_problem(
        await client.post("/v1/orders", json=order_body(items=_items(sku))),
        422,
        "validation-error",
    )
    assert problem["errors"], "ожидается список ошибок валидации"
    assert all({"loc", "msg", "type"} <= set(error) for error in problem["errors"])


async def test_create_order_with_max_total(client: httpx.AsyncClient) -> None:
    body = order_body(items=_items(*(f"SKU-{i}" for i in range(100)), quantity=1000, price=10**12))
    response = await create(client, body)
    assert response.status_code == 201, response.text
    assert response.json()["total_kopecks"] == 10**17


async def test_items_keep_request_order(client: httpx.AsyncClient) -> None:
    skus = [f"SKU-{i}" for i in reversed(range(30))]
    created = (await create(client, order_body(items=_items(*skus)))).json()

    fetched = (await client.get(f"/v1/orders/{created['id']}")).json()
    [listed] = (await _paginated_orders_request(client, created["customer_id"])).json()["items"]

    assert [item["sku"] for item in fetched["items"]] == skus
    assert [item["sku"] for item in listed["items"]] == skus


async def test_list_orders_newest_first_with_all_items(client: httpx.AsyncClient) -> None:
    customer_id = str(uuid.uuid4())
    oldest = (await create(client, order_body(customer_id, items=_items("A-1", "A-2", "A-3")))).json()
    middle = (await create(client, order_body(customer_id, items=_items("B-1", "B-2", "B-3")))).json()
    newest = (await create(client, order_body(customer_id, items=_items("C-1")))).json()
    await create(client, order_body())  # чужой заказ не попадает в список

    response = await _paginated_orders_request(client, customer_id, limit=2)

    assert response.status_code == 200
    assert response.json() == {"items": [newest, middle], "next_cursor": None}
    assert oldest not in response.json()["items"]


async def test_list_orders_default_limit_from_settings(
    client_factory,
    settings: OrdersSettings,
) -> None:
    customer_id = str(uuid.uuid4())
    app_settings = settings.model_copy(update={"page_default_limit": 2})
    async with client_factory(app_settings=app_settings) as client:
        for _ in range(3):
            await create(client, order_body(customer_id))

        response = await _paginated_orders_request(client, customer_id)

    assert response.status_code == 200
    assert len(response.json()["items"]) == 2


async def test_list_orders_for_customer_without_orders(client: httpx.AsyncClient) -> None:
    response = await _paginated_orders_request(client, str(uuid.uuid4()))
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"limit": 0}, id="zero-limit"),
        pytest.param({"limit": 101}, id="huge-limit"),
        pytest.param({"limit": "abc"}, id="non-int-limit"),
        pytest.param({"customer_id": "not-a-uuid"}, id="bad-customer-id"),
    ],
)
async def test_list_orders_invalid_query_is_422(client: httpx.AsyncClient, params: dict[str, Any]) -> None:
    response = await client.get("/v1/orders", params={"customer_id": str(uuid.uuid4()), **params})
    assert_problem(response, 422, "validation-error")


async def test_list_orders_requires_customer_id(client: httpx.AsyncClient) -> None:
    assert_problem(await client.get("/v1/orders"), 422, "validation-error")


async def test_orders_with_unavailable_database_is_503(client_factory) -> None:
    app_settings = OrdersSettings(
        _env_file=None,
        env="test",
        database_url=SecretStr("postgresql+asyncpg://u:p@127.0.0.1:1/db"),
    )
    async with client_factory(app_settings=app_settings) as client:
        response = await client.get(f"/v1/orders/{uuid.uuid4()}", headers={"x-request-id": "req-503"})

    assert_problem(response, 503, "db-unavailable")
    assert response.headers["x-request-id"] == "req-503"


class _CommitFailedError(Exception): ...


async def _failing_commit_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    engine = request.state.engine
    async with engine.begin() as conn:
        yield conn
        raise _CommitFailedError


async def test_commit_failure_before_response(client_factory, capsys) -> None:
    customer_id = str(uuid.uuid4())
    body = order_body(customer_id=customer_id)
    async with client_factory(dependency_overrides={get_db_conn: _failing_commit_conn}) as client:
        response = await create(client, body)
        assert_problem(response, 500, "internal-error")

    [event] = [e for e in _read_log_events(capsys) if e["event"] == "unhandled_error"]
    assert event["exception"][0]["exc_type"] == "_CommitFailedError"

    # Проверяем что заказ действительно не создался
    async with client_factory() as client:
        response = await _paginated_orders_request(client, customer_id=customer_id)
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}


@pytest.mark.parametrize("orders_count", [1, 50])
async def test_paginated_orders_db_query_count_is_fixed(
    client_factory,
    orders_count: int,
) -> None:
    fixed_limit = 20
    customer_id = str(uuid.uuid4())

    async with client_factory() as client:
        for _ in range(orders_count):
            await create(client, order_body(customer_id=customer_id))

        with db_queries_count() as info:
            response = await _paginated_orders_request(client, customer_id, limit=fixed_limit)

    assert response.status_code == 200
    assert len(info.statements) == 2, info.statements
    assert len(response.json()["items"]) == min(orders_count, fixed_limit)
