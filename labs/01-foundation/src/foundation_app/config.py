from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import SettingsConfigDict

from labkit.settings import APP_SETTINGS_LOGGED_FIELDS_ALLOW_LIST, AppSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent
FOUNDATION_SETTINGS_FIELDS_ALLOW_LIST = {
    "database_driver",
    "database_user",
    "database_host",
    "database_port",
    "database_name",
    *APP_SETTINGS_LOGGED_FIELDS_ALLOW_LIST,
}


class FoundationSettings(AppSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        env_file=BASE_DIR / ".env",
        frozen=True,
    )

    api_token: SecretStr
    http_timeout_s: float = Field(gt=0, le=30)


@lru_cache(maxsize=1)
def get_settings() -> FoundationSettings:
    return FoundationSettings()
