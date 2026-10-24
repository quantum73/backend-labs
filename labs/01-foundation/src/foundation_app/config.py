from functools import lru_cache
from pathlib import Path

from pydantic_settings import SettingsConfigDict

from labkit.settings import AppSettings

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class FoundationSettings(AppSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        env_file=BASE_DIR / ".env",
        frozen=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> FoundationSettings:
    return FoundationSettings()
