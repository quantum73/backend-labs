import sys

import structlog
from fastapi import FastAPI
from pydantic import ValidationError

from labkit.logging import configure_logging
from labkit.settings import summarize_validation_errors
from orders_app.main import create_app
from orders_app.settings import OrdersSettings

BAD_EXIT_CODE = 2


def entrypoint() -> FastAPI:
    """
    Точка сборки процесса: читает настройки из env, настраивает логирование и собирает приложение.
    Побочные эффекты (env, глобальная настройка structlog, sys.exit) допустимы только здесь,
    поэтому их нет в create_app.
    Вызывается один раз на воркер: uvicorn orders_app.entrypoint:entrypoint --factory
    """
    try:
        settings = OrdersSettings()
    except ValidationError as err:
        configure_logging(level="ERROR", json=True)
        structlog.get_logger().error("config_invalid", errors=summarize_validation_errors(err))
        sys.exit(BAD_EXIT_CODE)
    else:
        configure_logging(level=settings.log_level, json=settings.log_json)
        return create_app(settings)
