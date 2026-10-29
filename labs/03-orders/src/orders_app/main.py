import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import create_async_engine

from orders_app.api.health import health_router
from orders_app.db import DB_UNAVAILABLE_ERRORS, Database
from orders_app.errors import install_problem_handlers
from orders_app.middleware import RequestContextMiddleware
from orders_app.settings import OrdersSettings

METADATA_CREATE_TIMEOUT_S = 2


def create_app(settings: OrdersSettings) -> FastAPI:
    """
    Логирование - глобальное состояние процесса, а фабрика может вызываться много раз:
    - Если создать create_app(settings_a) с log_level=INFO, а потом create_app(settings_b) с ERROR,
    у первого приложения уровень логов тоже станет ERROR.
    - Логгер, который уже успел записать событие, например self.log в middleware,
    остаётся с прежней конфигурацией после повторного configure из-за кэша логгера.
    Поэтому configure_logging выносим в точку входа - src/orders_app/entrypoint.py::entrypoint.
    Теперь create_app требует явной настройки, а для запуска сервера будет использовать entrypoint;
    """

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[dict[str, Any]]:
        """
        Недоступная БД на старте не роняет процесс. Он жив (/livez 200), но не готов (/readyz 503).
        - Fail fast в k8s даёт CrashLoopBackOff: паузы между рестартами растут до 5 минут,
          и после восстановления БД под возвращается в строй медленнее, чем если просто дождаться готовности.
        - Схему, которую не удалось создать здесь, досоздаёт /readyz. Пока её нет, трафик на под не идёт.
        """
        engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            pool_pre_ping=True,
            connect_args=settings.db_connect_args,
        )
        database = Database(engine)
        try:
            async with asyncio.timeout(METADATA_CREATE_TIMEOUT_S):
                await database.ensure_schema()
        except DB_UNAVAILABLE_ERRORS:
            structlog.get_logger().exception(
                "db_unavailable_on_start",
                db_url=settings.masked_database_url,
                connect_args=settings.db_connect_args,
            )

        try:
            yield {"engine": engine, "database": database, "settings": settings}
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Orders API",
        version="1.0.0",
        lifespan=lifespan,
    )
    install_problem_handlers(app)
    app.add_middleware(RequestContextMiddleware)
    app.include_router(health_router)
    return app
