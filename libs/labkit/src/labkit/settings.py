from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    database_url: PostgresDsn
    api_token: SecretStr
    http_timeout_s: float = Field(default=2.0, gt=0, le=30)


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    return AppSettings()
