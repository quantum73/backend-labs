from typing import Annotated, Any

import sqlalchemy
from pydantic import Field, PostgresDsn, SecretStr, TypeAdapter, ValidationError, field_validator

from labkit.settings import AppSettings

ASYNCPG_DRIVER_NAME = "postgresql+asyncpg"

DbPoolSize = Annotated[int, Field(default=10, ge=1)]
DbStatementTimeoutMs = Annotated[int, Field(default=2000, ge=1)]
IdempotencyTTLs = Annotated[int, Field(default=86400, ge=60)]
PageDefaultLimit = Annotated[int, Field(default=20, ge=1, le=100)]


class OrdersSettings(AppSettings):
    # Обязательное, чтобы в проде пустой DSN не подменился тихо на localhost. Локальное значение в .env.example
    database_url: SecretStr
    # Выбирать db_pool_size нужно так, чтобы выполнялась формула: workers × pool_size × число подов < max_connections
    # Иначе получим от Postgres ошибки вида "FATAL: sorry, too many clients already".
    # Причем формула должна выполняться с запасом.
    # К примеру: значение workers × pool_size × число подов должно быть <=80% от значения max_connections.
    db_pool_size: DbPoolSize
    # Страховка от зависших SQL-запросов
    db_statement_timeout_ms: DbStatementTimeoutMs
    idempotency_ttl_s: IdempotencyTTLs
    page_default_limit: PageDefaultLimit

    @field_validator("database_url", mode="after")
    @classmethod
    def validate_database_url(cls, value: SecretStr) -> SecretStr:
        try:
            dsn = TypeAdapter(PostgresDsn).validate_python(value.get_secret_value())
        except ValidationError:
            raise ValueError("`database_url` is not a valid PostgreSQL DSN") from None

        if dsn.scheme != ASYNCPG_DRIVER_NAME:
            raise ValueError(f"`database_url` must use the {ASYNCPG_DRIVER_NAME} driver")

        return value

    @property
    def db_connect_args(self) -> dict[str, Any]:
        return {"server_settings": {"statement_timeout": str(self.db_statement_timeout_ms)}}

    @property
    def masked_database_url(self) -> str:
        db_url = sqlalchemy.engine.make_url(self.database_url.get_secret_value())
        return db_url.render_as_string(hide_password=True)
