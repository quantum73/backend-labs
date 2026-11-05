from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass
from typing import Annotated, NoReturn

import sqlalchemy.exc
import structlog
from fastapi import Depends, Request
from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    SmallInteger,
    Table,
    Text,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from orders_app.core.errors import DatabaseUnavailableError

DB_UNAVAILABLE_ERRORS = (sqlalchemy.exc.SQLAlchemyError, TimeoutError, OSError)
DB_CONNECT_ERRORS = (OSError, sqlalchemy.exc.TimeoutError)
# Сервер принял TCP-соединение, но отказал в сессии. Это временная недоступность, а не баг.
# 53300 too_many_connections — исчерпан max_connections или лимит роли.
# 57P03 cannot_connect_now — БД стартует или восстанавливается после сбоя.
DB_UNAVAILABLE_SQLSTATES = frozenset({"53300", "57P03"})

logger = structlog.get_logger()

metadata = MetaData()

orders = Table(
    "orders",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("customer_id", Uuid, nullable=False),
    Column("status", Text, nullable=False),
    Column("currency", Text, nullable=False),
    Column("total_kopecks", BigInteger, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index("ix_orders_customer_created_id", orders.c.customer_id, orders.c.created_at.desc(), orders.c.id.desc())

order_items = Table(
    "order_items",
    metadata,
    Column("order_id", Uuid, ForeignKey("orders.id", ondelete="CASCADE"), primary_key=True),
    Column("line_no", Integer, primary_key=True),
    Column("sku", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price_kopecks", BigInteger, nullable=False),
)

idempotency_keys = Table(
    "idempotency_keys",
    metadata,
    Column("scope", Text, primary_key=True),
    Column("key", Text, primary_key=True),
    Column("request_hash", Text, nullable=False),
    Column("response_status", SmallInteger, nullable=True),
    Column("response_body", JSONB, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "(response_status IS NULL) = (response_body IS NULL)",
        name="ck_idempotency_keys_response_complete",
    ),
)
Index("ix_idempotency_keys_expires_at", idempotency_keys.c.expires_at)


@dataclass
class Database:
    """
    Изменяемое состояние БД для lifespan state.
    Starlette копирует словарь state в каждый запрос поверхностно, поэтому флаг храним в объекте:
    если положить в state голый bool, его изменение в одном запросе не увидят другие.
    """

    engine: AsyncEngine
    schema_ready: bool = False

    async def ensure_schema(self) -> None:
        """
        Создаёт схему, если она ещё не создана. create_all идемпотентен.
        Гонка двух одновременных вызовов безопасна:
        проигравший получит ошибку, а следующий вызов увидит готовую схему.
        Таймаут задаёт вызывающий код.
        """
        if self.schema_ready:
            return

        async with self.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)

        self.schema_ready = True


def _raise_db_unavailable(exc: Exception) -> NoReturn:
    # При сбое БД трейсбек не пишем, чтобы не забить логи
    logger.warning("db_unavailable", error_type=type(exc).__name__, error=str(exc))
    raise DatabaseUnavailableError(detail="Could not get a database connection") from exc


async def get_db_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    """
    Одна транзакция на запрос: всё, что делает обработчик, коммитится или откатывается вместе.
    DBConn объявлен со scope="function", поэтому commit выполняется после обработчика, но ДО отправки ответа:
    упавший commit превращается в 500, а не в ложный 201 (со scope по умолчанию ответ ушёл бы раньше commit).
    Ошибки получения соединения переводятся в 503 здесь, на границе с БД, а не глобальным обработчиком.
    """
    engine: AsyncEngine = request.state.engine
    async with AsyncExitStack() as stack:
        try:
            conn = await stack.enter_async_context(engine.begin())
        except DB_CONNECT_ERRORS as exc:
            _raise_db_unavailable(exc)
        except sqlalchemy.exc.DBAPIError as exc:
            # Остальные ошибки сервера (например, неверный пароль) - это ошибка конфигурации, 500
            if getattr(exc.orig, "sqlstate", None) not in DB_UNAVAILABLE_SQLSTATES:
                raise
            _raise_db_unavailable(exc)

        yield conn


DBConn = Annotated[AsyncConnection, Depends(get_db_conn, scope="function")]
