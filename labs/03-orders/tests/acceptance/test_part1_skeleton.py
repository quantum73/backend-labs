"""Часть 1: каркас приложения — health, request id, Problem Details для ошибок фреймворка."""

import httpx
import pytest

from .helpers import assert_problem

pytestmark = pytest.mark.asyncio


async def test_livez(client: httpx.AsyncClient) -> None:
    response = await client.get("/livez")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_checks_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200


async def test_request_id_is_generated(client: httpx.AsyncClient) -> None:
    first = await client.get("/livez")
    second = await client.get("/livez")
    assert first.headers["x-request-id"]
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_valid_incoming_request_id_is_echoed(client: httpx.AsyncClient) -> None:
    response = await client.get("/livez", headers={"X-Request-ID": "abc-123_DEF.9"})
    assert response.headers["x-request-id"] == "abc-123_DEF.9"


@pytest.mark.parametrize("bad", ["", "a" * 200, "evil\u0001id", "with space"])
async def test_invalid_incoming_request_id_is_replaced(client: httpx.AsyncClient, bad: str) -> None:
    response = await client.get("/livez", headers={"X-Request-ID": bad.encode("latin-1").decode("latin-1")})
    assert response.headers["x-request-id"] != bad
    assert 1 <= len(response.headers["x-request-id"]) <= 64


async def test_unknown_route_is_problem(client: httpx.AsyncClient) -> None:
    response = await client.get("/no-such-route")
    problem = assert_problem(response, 404)
    assert problem["instance"] == "/no-such-route"
    assert response.headers["x-request-id"]


async def test_method_not_allowed_is_problem(client: httpx.AsyncClient) -> None:
    response = await client.delete("/livez")
    assert_problem(response, 405)
    assert "GET" in response.headers["allow"]
