from typing import Annotated, Any

import sqlalchemy
from pydantic import Field, PostgresDsn, SecretStr, TypeAdapter, ValidationError, field_validator, model_validator

from labkit.settings import AppSettings

ASYNCPG_DRIVER_NAME = "postgresql+asyncpg"

DbPoolSize = Annotated[int, Field(default=10, ge=1)]
DbStatementTimeoutMs = Annotated[int, Field(default=2000, ge=1)]
DbLockTimeoutMs = Annotated[int, Field(default=1000, ge=1)]
DbConnectTimeoutS = Annotated[float, Field(default=1.0, gt=0)]
DbPoolTimeoutS = Annotated[float, Field(default=1.0, gt=0)]
IdempotencyTTLs = Annotated[int, Field(default=86400, ge=60)]
MaxRequestBodyBytes = Annotated[int, Field(default=64 * 1024, ge=1024)]
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
    # Тауймаут на ожидание блокировки. Должен быть меньше statement_timeout,
    # чтобы ожидание блокировки заканчивалось своей ошибкой 55P03 → 409, а не общим statement_timeout → 500.
    db_lock_timeout_ms: DbLockTimeoutMs
    # Худший случай запроса к БД: pool_timeout + connect_timeout + statement_timeout (1 + 1 + 2 = 4 с).
    # Общий таймаут должен быть меньше таймаута клиента/балансировщика (5–10 с), иначе клиент уйдёт с 504,
    # а воркер будет работать впустую.
    db_connect_timeout_s: DbConnectTimeoutS
    db_pool_timeout_s: DbPoolTimeoutS
    idempotency_ttl_s: IdempotencyTTLs
    page_default_limit: PageDefaultLimit
    # Самый большой допустимый заказ (100 позиций) занимает около 12 КБ JSON, 64 КБ - с запасом.
    # Без лимита сервер читает в память и разбирает тело любого размера, прежде чем валидация его отклонит.
    max_request_body_bytes: MaxRequestBodyBytes

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

    @model_validator(mode="after")
    def validate_lock_timeout(self) -> OrdersSettings:
        if self.db_lock_timeout_ms >= self.db_statement_timeout_ms:
            raise ValueError("`db_lock_timeout_ms` must be less than `db_statement_timeout_ms`")
        return self

    @property
    def db_connect_args(self) -> dict[str, Any]:
        return {
            "timeout": self.db_connect_timeout_s,
            "server_settings": {
                "statement_timeout": str(self.db_statement_timeout_ms),
                "lock_timeout": str(self.db_lock_timeout_ms),
            },
        }

    @property
    def masked_database_url(self) -> str:
        db_url = sqlalchemy.engine.make_url(self.database_url.get_secret_value())
        return db_url.render_as_string(hide_password=True)
