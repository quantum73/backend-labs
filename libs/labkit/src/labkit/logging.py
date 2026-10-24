import copy
import logging
from typing import Any

import structlog
from structlog.typing import EventDict, WrappedLogger

SENSITIVE_FIELDS = {"password", "token", "secret", "authorization", "api_key", "card"}
MASKED_FIELD = "***"


def _mask_recursive(data: Any) -> Any:
    if isinstance(data, dict):
        new_dict = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            if any(substring == key_lower for substring in SENSITIVE_FIELDS):
                new_dict[key] = MASKED_FIELD
            else:
                new_dict[key] = _mask_recursive(value)
        return new_dict
    elif isinstance(data, list):
        return [_mask_recursive(item) for item in data]
    else:
        return copy.deepcopy(data)


def mask_sensitive(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    for key, value in list(event_dict.items()):
        if key not in ("event", "timestamp", "level"):  # Пропускаем служебные поля structlog
            event_dict[key] = _mask_recursive(value)
    return event_dict


def configure_logging(*, level: str = "INFO", json: bool = True) -> None:
    """Вызывается ОДИН раз в точке входа приложения. Библиотеки логирование не настраивают."""
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,  # поля из контекста: request_id, user_id
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        mask_sensitive,
    ]
    renderer: list[structlog.typing.Processor] = (
        [structlog.processors.dict_tracebacks, structlog.processors.JSONRenderer()]
        if json
        else [structlog.dev.ConsoleRenderer()]
    )
    structlog.configure(
        processors=[*shared, *renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        cache_logger_on_first_use=True,
    )
