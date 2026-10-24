import logging
from typing import Any

import structlog
from structlog.processors import ExceptionRenderer
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, WrappedLogger

SENSITIVE_FIELDS = {"password", "token", "secret", "authorization", "api_key", "card"}
MASKED_FIELD = "***"


def _mask_recursive(data: Any) -> Any:
    if isinstance(data, dict):
        new_dict = {}
        for key, value in data.items():
            key_lower = str(key).lower()
            is_sensitive = any(sub in key_lower for sub in SENSITIVE_FIELDS)
            if is_sensitive:
                new_dict[key] = MASKED_FIELD
            else:
                new_dict[key] = _mask_recursive(value)
        return new_dict

    elif isinstance(data, (list, tuple)):
        return [_mask_recursive(item) for item in data]
    else:
        return data


def mask_sensitive(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    try:
        new_event_dict = {}

        for key, value in event_dict.items():
            if key in ("event", "timestamp", "level"):
                new_event_dict[key] = value
                continue

            key_lower = str(key).lower()
            is_sensitive = any(sub in key_lower for sub in SENSITIVE_FIELDS)
            if is_sensitive:
                new_event_dict[key] = MASKED_FIELD
            else:
                new_event_dict[key] = _mask_recursive(value)
    except Exception:
        return event_dict
    else:
        return new_event_dict


def configure_logging(*, level: str = "INFO", json: bool = True) -> None:
    """Вызывается ОДИН раз в точке входа приложения. Библиотеки логирование не настраивают."""
    shared: list[structlog.typing.Processor] = [
        structlog.contextvars.merge_contextvars,  # поля из контекста: request_id, user_id
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        mask_sensitive,
    ]
    renderer: list[structlog.typing.Processor] = (
        [ExceptionRenderer(ExceptionDictTransformer(show_locals=False)), structlog.processors.JSONRenderer()]
        if json
        else [structlog.dev.ConsoleRenderer()]
    )
    structlog.configure(
        processors=[*shared, *renderer],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        cache_logger_on_first_use=True,
    )
