from typing import Annotated

from pydantic import Field, SecretStr

from labkit.settings import AppSettings


class CheckoutSettings(AppSettings):
    # Обязательное: в DSN пароль, значения по умолчанию нет (урок Лабы 1)
    database_url: SecretStr
    db_pool_size: Annotated[int, Field(ge=1)] = 10
    # Сервис уведомлений (письма клиентам). В тестах подменяется транспортом httpx
    notifier_url: str = "http://localhost:8081"
    # Общий дедлайн вызова сервиса уведомлений. Legacy его не использует, понадобится в части 4
    notifier_timeout_s: Annotated[float, Field(gt=0)] = 1.0
