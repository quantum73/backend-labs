"""Общие фикстуры лабы 4. Приёмочные тесты из tests/acceptance/ — эталон, их не меняем.

Контракт, на который опираются фикстуры (он не меняется до конца лабы):
- checkout_app.settings.CheckoutSettings — настройки приложения;
- checkout_app.main.create_app(settings, *, notifier_transport=None) -> FastAPI;
  при старте (lifespan) приложение само создаёт схему БД;
- схема БД (таблицы products, promo_codes, promo_redemptions, orders, order_lines и их колонки):
  тесты наполняют её напрямую. Добавлять колонки можно, переименовывать и удалять — нет.

PostgreSQL для тестов:
- по умолчанию поднимается контейнер через testcontainers (нужен запущенный Docker);
- либо задай CHECKOUT_TEST_DATABASE_URL (например, PostgreSQL из compose.yaml):
  CHECKOUT_TEST_DATABASE_URL=postgresql+asyncpg://checkout:checkout@localhost:5434/checkout
"""

import os
from collections.abc import AsyncIterator, Iterator
from typing import TYPE_CHECKING

import httpx
import pytest
import pytest_asyncio
import structlog
from asgi_lifespan import LifespanManager
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from checkout_tests.acceptance.helpers import FakeNotifier, Seed
from labkit.logging import configure_logging

if TYPE_CHECKING:
    from checkout_app.settings import CheckoutSettings


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    if url := os.environ.get("CHECKOUT_TEST_DATABASE_URL"):
        yield url
        return

    from testcontainers.community.postgres import PostgresContainer  # noqa: PLC0415 — нужен только без внешней БД

    with PostgresContainer("postgres:18-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture
def settings(database_url: str) -> CheckoutSettings:
    from checkout_app.settings import CheckoutSettings  # noqa: PLC0415

    return CheckoutSettings(
        _env_file=None,
        env="test",
        database_url=SecretStr(database_url),
        notifier_url="http://notifier.test",
        notifier_timeout_s=0.5,
    )


@pytest.fixture(autouse=True)
def _logging() -> Iterator[None]:
    # Без зависимости от settings: юнит-тестам домена и сценариев не нужны ни БД, ни Docker
    configure_logging(level="INFO", json=True)
    yield
    structlog.contextvars.clear_contextvars()


@pytest_asyncio.fixture
async def db(database_url: str) -> AsyncIterator[AsyncEngine]:
    """Отдельный engine для наполнения БД и проверок мимо API."""
    engine = create_async_engine(database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def notifier(db: AsyncEngine) -> FakeNotifier:
    return FakeNotifier(db)


@pytest_asyncio.fixture
async def client(settings: CheckoutSettings, notifier: FakeNotifier) -> AsyncIterator[httpx.AsyncClient]:
    from checkout_app.main import create_app  # noqa: PLC0415

    app = create_app(settings, notifier_transport=httpx.MockTransport(notifier.handle))
    async with LifespanManager(app) as manager:
        # raise_app_exceptions=False: необработанная ошибка приложения приходит как ответ 500, как у настоящего сервера
        transport = httpx.ASGITransport(app=manager.app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://checkout.test") as http:
            yield http


@pytest.fixture
def seed(client: httpx.AsyncClient, db: AsyncEngine) -> Seed:
    # Зависимость от client: схему создаёт lifespan приложения
    del client
    return Seed(db)
