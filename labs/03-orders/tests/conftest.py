"""Общие фикстуры лабы 3. Приёмочные тесты из tests/acceptance/ — эталон, их не меняем.

Контракт, на который опираются фикстуры (его реализуешь ты):
- orders_app.settings.OrdersSettings — наследник labkit AppSettings с полем database_url: SecretStr;
- orders_app.main.create_app(settings: OrdersSettings) -> FastAPI — фабрика приложения;
  при старте (lifespan) приложение само создаёт схему БД.

PostgreSQL для тестов:
- по умолчанию поднимается контейнер через testcontainers (нужен запущенный Docker);
- либо задай ORDERS_TEST_DATABASE_URL (например, PostgreSQL из compose.yaml) — так быстрее при частых прогонах:
  ORDERS_TEST_DATABASE_URL=postgresql+asyncpg://orders:orders@localhost:5433/orders
"""

import contextlib
import os
from collections.abc import AsyncIterator, Callable, Iterator
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import structlog
from asgi_lifespan import LifespanManager
from fastapi import APIRouter
from pydantic import SecretStr

from labkit.logging import configure_logging

if TYPE_CHECKING:
    from orders_app.settings import OrdersSettings


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    if url := os.environ.get("ORDERS_TEST_DATABASE_URL"):
        yield url
        return

    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415 — нужен только без внешней БД

    with PostgresContainer("postgres:18-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture
def settings(database_url: str) -> OrdersSettings:
    # Ленивый импорт: пока приложения нет (часть 0), conftest не должен ломать сбор тестов
    from orders_app.settings import OrdersSettings  # noqa: PLC0415

    return OrdersSettings(_env_file=None, env="test", database_url=SecretStr(database_url))


@pytest.fixture
def logging_configured(settings: OrdersSettings) -> None:
    configure_logging(level=settings.log_level, json=settings.log_json)


@pytest_asyncio.fixture
async def client(settings: OrdersSettings, logging_configured: None) -> AsyncIterator[httpx.AsyncClient]:
    from orders_app.main import create_app  # noqa: PLC0415

    app = create_app(settings)
    async with LifespanManager(app) as manager:
        transport = httpx.ASGITransport(app=manager.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://orders.test") as http:
            yield http


@pytest.fixture
def client_factory(
    settings: OrdersSettings,
    logging_configured: None,
) -> Callable[..., contextlib.AbstractAsyncContextManager[httpx.AsyncClient]]:
    @contextlib.asynccontextmanager
    async def _make_client(
        app_settings: OrdersSettings | None = None,
        custom_routers: list[APIRouter] | None = None,
    ) -> AsyncIterator[httpx.AsyncClient]:
        from orders_app.main import create_app  # noqa: PLC0415

        app_settings = app_settings if app_settings else settings
        app = create_app(app_settings)
        if custom_routers:
            for router in custom_routers:
                app.include_router(router)

        async with LifespanManager(app) as manager:
            transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
            async with httpx.AsyncClient(transport=transport, base_url="http://orders.test") as http:
                yield http

    return _make_client


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("APP_"):
            monkeypatch.delenv(key)

    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()

    yield

    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()
