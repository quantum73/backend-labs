import asyncio

from fastapi import APIRouter, Request, status
from sqlalchemy import text

from orders_app.db import DB_UNAVAILABLE_ERRORS, Database
from orders_app.errors import DBNotReadyError

health_router = APIRouter(tags=["Health"])

DATABASE_CHECK_TIMEOUT_S = 1
_OK_RESPONSE_PAYLOAD = {"status": "ok"}


@health_router.get("/livez", status_code=status.HTTP_200_OK)
async def livez() -> dict[str, str]:
    return _OK_RESPONSE_PAYLOAD


@health_router.get("/readyz", status_code=status.HTTP_200_OK)
async def readyz(request: Request) -> dict[str, str]:
    """
    Готовность = БД отвечает и схема создана.
    Если БД была недоступна на старте, схему досоздаём здесь.
    Без этого после восстановления БД под стал бы «готов», а запросы к несуществующим таблицам падали бы с 500.
    """
    database: Database = request.state.database
    try:
        async with asyncio.timeout(DATABASE_CHECK_TIMEOUT_S):
            await database.ensure_schema()
            async with database.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except DB_UNAVAILABLE_ERRORS as exc:
        raise DBNotReadyError(detail="Database health check failed") from exc
    else:
        return _OK_RESPONSE_PAYLOAD
