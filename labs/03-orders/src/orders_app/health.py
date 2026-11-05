import asyncio

from dishka import FromDishka
from dishka.integrations.fastapi import DishkaRoute
from fastapi import APIRouter, status
from sqlalchemy import text

from orders_app.core.errors import DBNotReadyError
from orders_app.db import DB_UNAVAILABLE_ERRORS, Database

router = APIRouter(tags=["Health"], route_class=DishkaRoute)

DATABASE_CHECK_TIMEOUT_S = 1
_OK_RESPONSE_PAYLOAD = {"status": "ok"}


@router.get("/livez", status_code=status.HTTP_200_OK)
async def livez() -> dict[str, str]:
    return _OK_RESPONSE_PAYLOAD


@router.get("/readyz", status_code=status.HTTP_200_OK)
async def readyz(database: FromDishka[Database]) -> dict[str, str]:
    """
    Готовность = БД отвечает и схема создана.
    Если БД была недоступна на старте, схему досоздаём здесь.
    Без этого после восстановления БД под стал бы «готов»,
    а запросы к несуществующим таблицам падали бы с 500.
    Пока лаба учебная, так можно, но в продакшене readiness-проба не должна иметь побочных эффектов:
    k8s вызывает её каждые несколько секунд на каждом поде,
    а DDL из проб нескольких подов одновременно — это гонка.
    В проде схему меняют миграции отдельным шагом, это разбирается в Модуле 7.
    """
    try:
        async with asyncio.timeout(DATABASE_CHECK_TIMEOUT_S):
            await database.ensure_schema()
            async with database.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
    except DB_UNAVAILABLE_ERRORS as exc:
        raise DBNotReadyError(detail="Database health check failed") from exc
    else:
        return _OK_RESPONSE_PAYLOAD
