"""Контракт OpenAPI: то, что видят клиенты API."""

import httpx
import pytest

pytestmark = pytest.mark.asyncio


async def test_openapi_documents_idempotency_and_problems(client: httpx.AsyncClient) -> None:
    spec = (await client.get("/openapi.json")).json()
    create = spec["paths"]["/v1/orders"]["post"]

    header_params = [p for p in create.get("parameters", []) if p["in"] == "header"]
    assert any(p["name"].lower() == "idempotency-key" for p in header_params)
    assert "201" in create["responses"]
    for status in ("400", "422"):
        assert "application/problem+json" in create["responses"][status]["content"]

    get_one = spec["paths"]["/v1/orders/{order_id}"]["get"]
    assert "application/problem+json" in get_one["responses"]["404"]["content"]
