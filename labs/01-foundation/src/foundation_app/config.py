from functools import lru_cache
from typing import Self

from pydantic import Field, PostgresDsn, SecretStr, ValidationError, model_validator

from labkit.settings import AppSettings

LOGGED_SETTINGS_FIELDS = frozenset(
    {
        "env",
        "log_level",
        "log_json",
        "database_driver",
        "database_user",
        "database_host",
        "database_port",
        "database_name",
        "http_timeout_s",
    }
)


class FoundationSettings(AppSettings):
    database_driver: str = "postgresql"
    database_user: str
    database_password: SecretStr
    database_host: str
    database_port: int = Field(ge=1, le=65535)
    database_name: str
    api_token: SecretStr
    http_timeout_s: float = Field(default=2.0, gt=0, le=30)

    @model_validator(mode="after")
    def _check_database_dsn(self) -> Self:
        self.database_dsn()
        return self

    def database_dsn(self) -> SecretStr:
        try:
            dsn = PostgresDsn.build(
                scheme=self.database_driver,
                username=self.database_user,
                password=self.database_password.get_secret_value(),
                host=self.database_host,
                port=self.database_port,
                path=self.database_name,
            )
            return SecretStr(str(dsn))
        except ValidationError:
            pass

        raise ValueError("database_* fields do not form a valid PostgreSQL DSN") from None


@lru_cache(maxsize=1)
def get_settings() -> FoundationSettings:
    return FoundationSettings()  # pyright: ignore[reportCallIssue]
