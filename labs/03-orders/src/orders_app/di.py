from collections.abc import AsyncIterator

import sqlalchemy.exc
import structlog
from dishka import Provider, Scope, from_context, provide
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from orders_app.db import DB_CONNECT_ERRORS, DB_UNAVAILABLE_SQLSTATES, Database, raise_db_unavailable
from orders_app.settings import OrdersSettings


class InfraProvider(Provider):
    # Настройки собирает entrypoint и передаёт в контейнер: провайдеры не читают env сами
    settings = from_context(provides=OrdersSettings, scope=Scope.APP)

    @provide(scope=Scope.APP)
    async def engine(self, settings: OrdersSettings) -> AsyncIterator[AsyncEngine]:
        engine = create_async_engine(
            settings.database_url.get_secret_value(),
            pool_size=settings.db_pool_size,
            max_overflow=0,
            pool_timeout=settings.db_pool_timeout_s,
            pool_pre_ping=True,
            connect_args=settings.db_connect_args,
        )
        yield engine
        # Scope.APP закрывается в shutdown lifespan (container.close()), так что пул закрывается как раньше
        await engine.dispose()
        structlog.get_logger().info("db_pool_closed")

    @provide(scope=Scope.APP)
    def database(self, engine: AsyncEngine) -> Database:
        return Database(engine)

    @provide(scope=Scope.REQUEST)
    async def connection(self, engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
        """
        Только соединение, БЕЗ commit.
        Finalizer Scope.REQUEST выполняется после отправки ответа и упавший там commit клиент бы не увидел.
        Транзакцию открывает и коммитит код приложения. Незакоммиченное при закрытии соединения откатывается.
        """
        try:
            conn = await engine.connect()
        except DB_CONNECT_ERRORS as exc:
            raise_db_unavailable(exc)
        except sqlalchemy.exc.DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) not in DB_UNAVAILABLE_SQLSTATES:
                raise
            raise_db_unavailable(exc)

        try:
            yield conn
        finally:
            await conn.close()
