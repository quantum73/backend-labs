from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr, field_serializer
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_SETTINGS_LOGGED_FIELDS_ALLOW_LIST = {"env", "log_level", "log_json", "api_token", "http_timeout_s"}


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        env_file=".env",  # только для локальной разработки; в k8s приходят env-переменные
        frozen=True,  # конфиг неизменяем после старта
    )

    env: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True
    database_url: PostgresDsn | None = Field(default=None)
    database_driver: str = Field(default="postgresql")
    database_user: str
    database_password: SecretStr
    database_host: str
    database_port: int
    database_name: str
    api_token: SecretStr
    http_timeout_s: float = Field(default=2.0, gt=0, le=30)

    @field_serializer("database_url")
    def serialize_url(self, _: PostgresDsn) -> str:
        dsn = PostgresDsn.build(
            scheme=self.database_driver,
            username=self.database_user,
            password=self.database_password.get_secret_value(),
            host=self.database_host,
            port=self.database_port,
            path=self.database_name,
        )
        return str(dsn)


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()
