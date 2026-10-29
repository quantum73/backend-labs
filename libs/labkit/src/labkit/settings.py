from typing import Literal

from pydantic import ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        env_file=".env",  # только для локальной разработки; в k8s приходят env-переменные
        frozen=True,  # конфиг неизменяем после старта
        hide_input_in_errors=True,
    )

    env: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    log_json: bool = True


def summarize_validation_errors(error: ValidationError) -> list[dict[str, str]]:
    """
    Ошибки конфига для события config_invalid: поле, тип и сообщение, без введённых значений.
    Для того чтобы скрыть секреты include_input=False обязателен.
    """
    return [
        {
            "field": ".".join(str(part) for part in item["loc"]) if item["loc"] else "root",
            "type": item["type"],
            "msg": item["msg"],
        }
        for item in error.errors(include_input=False, include_url=False)
    ]
