from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import BigInteger, Column, DateTime, ForeignKey, Index, Integer, MetaData, Table, Text, Uuid
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

DB_UNAVAILABLE_ERRORS = (SQLAlchemyError, TimeoutError, OSError)

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


async def get_db_conn(request: Request) -> AsyncIterator[AsyncConnection]:
    engine: AsyncEngine = request.state.engine
    async with engine.begin() as conn:
        yield conn


DBConn = Annotated[AsyncConnection, Depends(get_db_conn, scope="function")]
