import logging
import sys
from typing import Any, TextIO

import structlog
from structlog.processors import ExceptionRenderer
from structlog.stdlib import ProcessorFormatter
from structlog.tracebacks import ExceptionDictTransformer
from structlog.typing import EventDict, WrappedLogger

SENSITIVE_FIELDS = frozenset({"password", "token", "secret", "authorization", "api_key", "card"})
MASKED_FIELD = "***"
_PASSTHROUGH_KEYS = frozenset({"event", "timestamp", "level"})
# Логгеры серверов приложений: у них свои обработчики и текстовый формат, отправляем их записи в root
_SERVER_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "_granian", "granian.access")


def _shared_processors() -> list[structlog.typing.Processor]:
    """Общие для structlog-событий и stdlib-записей: оба потока выглядят одинаково."""
    return [
        structlog.contextvars.merge_contextvars,  # request_id попадёт и в логи библиотек внутри запроса
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        mask_sensitive,
    ]


def build_formatter(*, json: bool = True) -> logging.Formatter:
    renderer: list[structlog.typing.Processor] = (
        [ExceptionRenderer(ExceptionDictTransformer(show_locals=False)), structlog.processors.JSONRenderer()]
        if json
        else [structlog.dev.ConsoleRenderer()]
    )
    return ProcessorFormatter(
        # Для записей stdlib: у них не было structlog-процессоров. ExtraAdder переносит поля из extra=
        foreign_pre_chain=[structlog.stdlib.ExtraAdder(), *_shared_processors()],
        processors=[ProcessorFormatter.remove_processors_meta, *renderer],
    )


def json_formatter() -> logging.Formatter:
    """Фабрика для dictConfig серверов: "()": "labkit.logging.json_formatter"."""
    return build_formatter(json=True)


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


class _CurrentStdoutHandler(logging.StreamHandler[TextIO]):
    """
    Пишет в sys.stdout, актуальный на момент записи, а не на момент настройки.
    StreamHandler(sys.stdout) запомнил бы поток при вызове configure_logging, и подмена sys.stdout после этого
    (capsys в тестах, перенаправление вывода) молча теряла бы логи. Так же вёл себя прежний PrintLoggerFactory.
    """

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, _value: TextIO) -> None:
        pass  # StreamHandler.__init__ присваивает stream; поток всегда берём из sys.stdout


def configure_logging(*, level: str = "INFO", json: bool = True, stream: TextIO | None = None) -> None:
    """Вызывается ОДИН раз в точке входа. Все логи процесса (наши и библиотек) уходят одним форматом."""
    handler = logging.StreamHandler(stream) if stream is not None else _CurrentStdoutHandler()
    handler.setFormatter(build_formatter(json=json))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    for name in _SERVER_LOGGERS:
        server_logger = logging.getLogger(name)
        server_logger.handlers.clear()
        server_logger.propagate = True

    structlog.configure(
        processors=[*_shared_processors(), ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelNamesMapping()[level]),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )
