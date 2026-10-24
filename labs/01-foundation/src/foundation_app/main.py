import uuid

import structlog
from pydantic import ValidationError

from foundation_app.config import LOGGED_SETTINGS_FIELDS, get_settings
from labkit.logging import configure_logging

GOOD_EXIT_CODE = 0
BAD_EXIT_CODE = 2


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as e:
        configure_logging(level="ERROR", json=True)
        logger = structlog.get_logger()

        errors_summary = []
        for error in e.errors(include_input=False, include_url=False):
            field_path = ".".join(str(x) for x in error["loc"]) if error["loc"] else "root"
            errors_summary.append({"field": field_path, "type": error["type"], "msg": error["msg"]})

        logger.error("config_invalid", errors=errors_summary)  # noqa: TRY400 — ожидаемая ошибка конфига, трейсбек не нужен
        return BAD_EXIT_CODE
    else:
        configure_logging(level=settings.log_level, json=settings.log_json)
        logger = structlog.get_logger()

        structlog.contextvars.bind_contextvars(run_id=str(uuid.uuid7()))
        logger.info("app_started", settings=settings.model_dump(mode="json", include=set(LOGGED_SETTINGS_FIELDS)))
        return GOOD_EXIT_CODE


if __name__ == "__main__":
    raise SystemExit(main())
