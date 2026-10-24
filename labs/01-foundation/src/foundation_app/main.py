import sys
import uuid

import structlog
from pydantic import ValidationError

from foundation_app.config import get_settings
from labkit.logging import configure_logging

logger = structlog.get_logger()


def main() -> None:
    try:
        json_settings = get_settings().model_dump(mode="json")

        configure_logging(level="INFO", json=True)

        current_run_id = str(uuid.uuid7())
        structlog.contextvars.bind_contextvars(run_id=current_run_id)

        logger.info("app_started", settings=json_settings)

        sys.exit(0)

    except ValidationError as e:
        configure_logging(level="ERROR", json=True)

        errors_summary = []
        for error in e.errors():
            loc = " -> ".join(str(x) for x in error["loc"])
            errors_summary.append({loc: error["msg"]})

        logger.exception("config_invalid", errors=errors_summary)

        sys.exit(2)


if __name__ == "__main__":
    main()
