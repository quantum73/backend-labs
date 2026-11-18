from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.ext.asyncio import create_async_engine

from checkout_app import legacy
from checkout_app.settings import CheckoutSettings


def create_app(
    settings: CheckoutSettings,
    *,
    notifier_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """
    Контракт для тестов (не меняется до конца лабы):
    - settings — настройки, собранные снаружи;
    - notifier_transport — транспорт httpx для сервиса уведомлений. None — настоящая сеть,
      в тестах — фейковый сервис уведомлений.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        engine = create_async_engine(settings.database_url.get_secret_value(), pool_size=settings.db_pool_size)
        async with engine.begin() as conn:
            await conn.run_sync(legacy.metadata.create_all)
        notifier = httpx.AsyncClient(base_url=settings.notifier_url, transport=notifier_transport)
        app.state.engine = engine
        app.state.notifier = notifier
        try:
            yield
        finally:
            await notifier.aclose()
            await engine.dispose()

    app = FastAPI(title="Checkout API", version="1.0.0", lifespan=lifespan)
    app.add_exception_handler(RequestValidationError, legacy.validation_error_handler)
    app.include_router(legacy.router)
    return app
