import uuid

import structlog
from pydantic import ValidationError

from foundation_app.config import get_settings
from labkit.config import APP_SETTINGS_LOGGED_FIELDS_ALLOW_LIST
from labkit.logging import configure_logging

logger = structlog.get_logger()
GOOD_EXIT_CODE = 0
BAD_EXIT_CODE = 2


def main() -> int:
    try:
        settings = get_settings()
    except ValidationError as e:
        configure_logging(level="ERROR", json=True)

        errors_summary = []
        for error in e.errors(include_input=False, include_url=False):
            field_path = ".".join(str(x) for x in error["loc"]) if error["loc"] else "root"
            errors_summary.append({"field": field_path, "type": error["type"], "msg": error["msg"]})

        logger.error("config_invalid", errors=errors_summary)  # noqa: TRY400
        return BAD_EXIT_CODE
    else:
        json_settings = settings.model_dump(mode="json", include=APP_SETTINGS_LOGGED_FIELDS_ALLOW_LIST)

        configure_logging(level=settings.log_level, json=settings.log_json)

        current_run_id = str(uuid.uuid7())
        structlog.contextvars.bind_contextvars(run_id=current_run_id)

        logger.info("app_started", settings=json_settings)
        return GOOD_EXIT_CODE


if __name__ == "__main__":
    main()
