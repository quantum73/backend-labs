import json
import uuid
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import APIRouter
from pydantic import SecretStr
from sqlalchemy import Column, Integer, Table, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from starlette.responses import JSONResponse

from orders_app import main
from orders_app.db import metadata
from orders_app.settings import OrdersSettings
from tests.acceptance.helpers import assert_problem

pytestmark = pytest.mark.asyncio


def _create_api_route(
    *,
    path: str,
    endpoint_handler: Callable[..., Any],
    methods: set[str] | list[str] | None = None,
) -> APIRouter:
    custom_router = APIRouter()
    custom_router.add_api_route(
        path=path,
        endpoint=endpoint_handler,
        methods=methods,
    )
    return custom_router


def _read_single_event(capsys) -> dict:
    out, err = capsys.readouterr()
    assert not err
    lines = out.splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


async def test_masked_route_in_access_log(client_factory, capsys) -> None:
    access_log_api_handler_path = "/access/log/{idx}"
    idx = str(uuid.uuid4())
    custom_router = _create_api_route(
        path=access_log_api_handler_path,
        endpoint_handler=lambda: JSONResponse({"status": "ok"}, status_code=200),
        methods=["GET"],
    )
    async with client_factory(custom_routers=[custom_router]) as client:
        response = await client.get(access_log_api_handler_path.format(idx=idx))

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

    log_event = _read_single_event(capsys)
    assert log_event["route"] == access_log_api_handler_path
    assert idx not in log_event["route"]


async def test_readyz_handler(client: httpx.AsyncClient) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_handler_with_unhandled_error(client_factory, capsys) -> None:
    def _broken():
        raise RuntimeError("Broken")

    broken_api_handler_path = "/broken"
    broken_router = _create_api_route(
        path=broken_api_handler_path,
        endpoint_handler=_broken,
        methods=["GET"],
    )
    request_id = "req-id-123"

    async with client_factory(custom_routers=[broken_router]) as client:
        response = await client.get(broken_api_handler_path, headers={"x-request-id": request_id})

    assert response.status_code == 500
    assert_problem(response=response, status=500, slug="internal-error")
    assert "Broken" not in response.text
    assert response.headers["x-request-id"] == request_id

    out, err = capsys.readouterr()
    assert not err
    lines = out.splitlines()
    assert len(lines) == 2

    log_events = [json.loads(line) for line in lines]
    assert all(log["request_id"] == request_id for log in log_events)

    [unhandled_log_event] = [log for log in log_events if log["event"] == "unhandled_error"]
    assert unhandled_log_event["request_id"] == request_id


async def test_health_handlers_with_unhealthy_database(client_factory, capsys) -> None:
    password = uuid.uuid4().hex
    database_url = f"postgresql+asyncpg://u:{password}@127.0.0.1:1/db"
    app_settings = OrdersSettings(_env_file=None, env="test", database_url=SecretStr(database_url))

    async with client_factory(app_settings=app_settings) as client:
        response = await client.get("/livez")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

        response = await client.get("/readyz")
        assert response.status_code == 503
        assert_problem(response=response, status=503, slug="not-ready")

    out, err = capsys.readouterr()
    assert not err
    assert password not in out

    log_events = [json.loads(line) for line in out.splitlines()]
    [db_unavailable_log] = [log for log in log_events if log["event"] == "db_unavailable_on_start"]
    assert db_unavailable_log["db_url"] == "postgresql+asyncpg://u:***@127.0.0.1:1/db"


async def test_readyz_creates_schema_if_start_failed(client_factory, settings, monkeypatch, capsys) -> None:
    table_name = f"readyz_probe_{uuid.uuid4().hex}"
    table = Table(table_name, metadata, Column("id", Integer, primary_key=True))
    # Нулевой таймаут: create_all на старте гарантированно не успевает, как при недоступной БД
    monkeypatch.setattr(main, "METADATA_CREATE_TIMEOUT_S", 0)
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        async with client_factory() as client:
            assert not await _table_exists(engine, table_name)

            response = await client.get("/readyz")

            assert response.status_code == 200
            assert await _table_exists(engine, table_name)
    finally:
        metadata.remove(table)
        async with engine.begin() as conn:
            await conn.execute(text(f'DROP TABLE IF EXISTS "{table_name}"'))
        await engine.dispose()

    events = [json.loads(line)["event"] for line in capsys.readouterr().out.splitlines()]
    assert "db_unavailable_on_start" in events


async def _table_exists(engine: AsyncEngine, table_name: str) -> bool:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT to_regclass(:name) IS NOT NULL"), {"name": table_name})
        return bool(result.scalar_one())
