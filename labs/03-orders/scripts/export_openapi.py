"""Выгружает OpenAPI-схему в labs/03-orders/openapi.json. Детерминированно: снимок в git не «шумит»."""

import json
import pathlib

from pydantic import SecretStr

from orders_app.main import create_app
from orders_app.settings import OrdersSettings

OUTPUT = pathlib.Path(__file__).parents[1] / "openapi.json"


def main() -> None:
    # Схема зависит только от кода, не от окружения; БД при генерации не нужна
    settings = OrdersSettings(_env_file=None, database_url=SecretStr("postgresql+asyncpg://u:p@localhost/db"))
    spec = create_app(settings).openapi()
    # sort_keys — стабильный порядок; ensure_ascii=False — русские описания читаемы в diff
    OUTPUT.write_text(json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
