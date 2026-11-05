"""Часть 4: список заказов с курсорной (keyset) пагинацией."""

import uuid
from typing import Any

import httpx
import pytest

from .helpers import assert_problem, create, order_body

pytestmark = pytest.mark.asyncio


async def _seed(client: httpx.AsyncClient, customer_id: str, count: int) -> list[str]:
    ids = []
    for _ in range(count):
        response = await create(client, order_body(customer_id))
        assert response.status_code == 201
        ids.append(response.json()["id"])
    return ids


async def _page(client: httpx.AsyncClient, **params: Any) -> dict[str, Any]:
    response = await client.get("/v1/orders", params=params)
    assert response.status_code == 200, response.text
    page: dict[str, Any] = response.json()
    return page


async def test_walks_all_pages_newest_first(client: httpx.AsyncClient) -> None:
    customer = str(uuid.uuid4())
    created = await _seed(client, customer, 25)

    seen: list[dict[str, Any]] = []
    page = await _page(client, customer_id=customer, limit=10)
    sizes = []
    while True:
        sizes.append(len(page["items"]))
        seen.extend(page["items"])
        if page["next_cursor"] is None:
            break
        page = await _page(client, customer_id=customer, limit=10, cursor=page["next_cursor"])

    assert sizes == [10, 10, 5]
    assert [o["id"] for o in seen] == list(reversed(created))
    keys = [(o["created_at"], o["id"]) for o in seen]
    assert keys == sorted(keys, reverse=True)


async def test_default_limit_is_20(client: httpx.AsyncClient) -> None:
    customer = str(uuid.uuid4())
    await _seed(client, customer, 21)
    page = await _page(client, customer_id=customer)
    assert len(page["items"]) == 20
    assert page["next_cursor"] is not None


async def test_exact_page_has_no_next_cursor(client: httpx.AsyncClient) -> None:
    customer = str(uuid.uuid4())
    await _seed(client, customer, 5)
    assert (await _page(client, customer_id=customer, limit=5))["next_cursor"] is None
    assert (await _page(client, customer_id=str(uuid.uuid4())))["items"] == []


async def test_inserts_between_pages_cause_no_duplicates_or_gaps(client: httpx.AsyncClient) -> None:
    customer = str(uuid.uuid4())
    original = await _seed(client, customer, 12)

    first = await _page(client, customer_id=customer, limit=5)
    await _seed(client, customer, 3)  # новые заказы появились между запросами страниц
    rest: list[str] = []
    page = await _page(client, customer_id=customer, limit=5, cursor=first["next_cursor"])
    while True:
        rest.extend(o["id"] for o in page["items"])
        if page["next_cursor"] is None:
            break
        page = await _page(client, customer_id=customer, limit=5, cursor=page["next_cursor"])

    walked = [o["id"] for o in first["items"]] + rest
    assert walked == list(reversed(original))


@pytest.mark.parametrize("limit", [0, 101, -1])
async def test_limit_out_of_range_is_422(client: httpx.AsyncClient, limit: int) -> None:
    response = await client.get("/v1/orders", params={"limit": limit})
    assert_problem(response, 422, "validation-error")


@pytest.mark.parametrize("cursor", ["garbage", "eyJ2IjoxfQ", "!!!"])
async def test_malformed_cursor_is_400(client: httpx.AsyncClient, cursor: str) -> None:
    response = await client.get("/v1/orders", params={"customer_id": str(uuid.uuid4()), "cursor": cursor})
    assert_problem(response, 400, "invalid-cursor")


async def test_cursor_is_bound_to_filters(client: httpx.AsyncClient) -> None:
    customer = str(uuid.uuid4())
    await _seed(client, customer, 3)
    cursor = (await _page(client, customer_id=customer, limit=1))["next_cursor"]
    response = await client.get("/v1/orders", params={"customer_id": str(uuid.uuid4()), "cursor": cursor})
    assert_problem(response, 400, "invalid-cursor")
