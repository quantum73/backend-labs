import logging
from typing import Any

import structlog
from structlog.processors import ExceptionRenderer
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, WrappedLogger

SENSITIVE_FIELDS = frozenset({"password", "token", "secret", "authorization", "api_key", "card"})
MASKED_FIELD = "***"
_PASSTHROUGH_KEYS = frozenset({"event", "timestamp", "level"})


def _is_sensitive(key: object) -> bool:
    normalized = str(key).lower().replace("-", "_")
    return any(marker in normalized for marker in SENSITIVE_FIELDS)


def _mask_recursive(data: Any) -> Any:
    if isinstance(data, dict):
        return {key: MASKED_FIELD if _is_sensitive(key) else _mask_recursive(value) for key, value in data.items()}
    if isinstance(data, list):
        return [_mask_recursive(item) for item in data]
    if isinstance(data, tuple):
        return tuple(_mask_recursive(item) for item in data)
    return data


def mask_sensitive(_: WrappedLogger, __: str, event_dict: EventDict) -> EventDict:
    passthrough = {key: value for key, value in event_dict.items() if key in _PASSTHROUGH_KEYS}
    payload = {key: value for key, value in event_dict.items() if key not in _PASSTHROUGH_KEYS}
    try:
        masked: dict[str, Any] = _mask_recursive(payload)
    except Exception as exc:
        # fail-closed: при ошибке теряем поля, но не раскрываем секреты; тип ошибки оставляем для отладки
        masked = dict.fromkeys(payload, MASKED_FIELD)
        masked |= {"masking_failed": True, "masking_error": type(exc).__name__}
    return passthrough | masked


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
