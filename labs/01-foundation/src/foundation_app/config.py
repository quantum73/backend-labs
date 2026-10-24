from functools import lru_cache

from pydantic_settings import SettingsConfigDict

from labkit.settings import AppSettings


class FoundationSettings(AppSettings):
    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_nested_delimiter="__",
        env_file=".env",
        frozen=True,
    )


@lru_cache(maxsize=1)
def get_settings() -> FoundationSettings:
    return FoundationSettings()
