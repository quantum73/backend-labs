from dataclasses import dataclass

from sqlalchemy import MetaData
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine

metadata = MetaData()

DB_UNAVAILABLE_ERRORS = (SQLAlchemyError, TimeoutError, OSError)


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
