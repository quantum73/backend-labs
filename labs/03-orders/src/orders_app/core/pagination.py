import base64
import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import Depends, Query
from pydantic import AfterValidator, AwareDatetime, BaseModel, ConfigDict, Strict

from orders_app.core.errors import InvalidCursorError
from orders_app.settings import OrdersSettings  # не под TYPE_CHECKING: Dishka читает тип из аннотации во время работы

CURSOR_VERSION = 1


# Синхронные зависимости FastAPI выполняет в пуле потоков (40 на процесс), а здесь нечему блокировать.
# @inject: DishkaRoute внедряет FromDishka только в сам маршрут, во вложенную зависимость — нет
@inject
async def get_page_limit_from_query(
    settings: FromDishka[OrdersSettings],
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> int:
    return settings.page_default_limit if limit is None else limit


def _to_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError:
        # OverflowError не подкласс ValueError, Pydantic сам его не перехватит → был бы 500
        raise ValueError("cursor timestamp is out of range") from None


class _CursorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: Annotated[int, Strict()]
    t: Annotated[AwareDatetime, Strict(), AfterValidator(_to_utc)]
    id: UUID
    f: str


class Cursor(BaseModel):
    created_at: AwareDatetime
    id: UUID


def filters_fingerprint(filters: dict[str, str]) -> str:
    filters_as_string = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(filters_as_string).hexdigest()


def encode_cursor(cursor: Cursor, *, filters_fp: str) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "t": cursor.created_at.isoformat(),
        "id": str(cursor.id),
        "f": filters_fp,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(raw: str, *, filters_fp: str) -> Cursor:
    """
    Клиент может раскодировать курсор и подставить любые t/id.
    В нашем случае это безопасно: курсор лишь задаёт позицию внутри данных того же клиента.
    Подпись (HMAC) здесь не нужна, потому что фильтр customer_id берётся из параметра запроса,
    а не из курсора. Подделанный курсор может сдвинуть позицию только внутри заказов того же клиента.
    """
    try:
        payload = _CursorPayload.model_validate_json(
            base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)),
        )
    # Достаточно ловить ошибку ValueError, потому что binascii.Error, UnicodeDecodeError,
    # pydantic.ValidationError - её подклассы.
    except ValueError as exc:  # битый base64/JSON/UUID/дата → 400, а не 500
        raise InvalidCursorError("Malformed cursor") from exc

    if payload.v != CURSOR_VERSION:
        raise InvalidCursorError("Unsupported cursor version")
    if payload.f != filters_fp:
        raise InvalidCursorError("Cursor was issued for different filters")

    return Cursor(created_at=payload.t, id=payload.id)


PageLimitFromQuery = Annotated[int, Depends(get_page_limit_from_query)]
