import base64
import hashlib
import json
from typing import TYPE_CHECKING, Annotated
from uuid import UUID

from fastapi import Depends, Query, Request
from pydantic import AwareDatetime, BaseModel, ConfigDict, Strict

from orders_app.core.errors import InvalidCursorError

if TYPE_CHECKING:
    from orders_app.settings import OrdersSettings

CURSOR_VERSION = 1
# Текущий закодированный курсор занимает ~210 символов, 512 - значение с запасом.
CURSOR_MAX_LENGTH = 512


# Синхронные зависимости FastAPI выполняет в пуле потоков (40 на процесс), а здесь нечему блокировать
async def get_page_limit_from_query(
    request: Request,
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
) -> int:
    settings: OrdersSettings = request.state.settings
    return settings.page_default_limit if limit is None else limit


class OrdersCursor(BaseModel):
    created_at: AwareDatetime
    id: UUID


class _OrdersCursorPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    v: Annotated[int, Strict()]
    t: Annotated[AwareDatetime, Strict()]
    id: UUID
    f: str


def filters_fingerprint(filters: dict[str, str]) -> str:
    filters_as_string = json.dumps(filters, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(filters_as_string).hexdigest()


def encode_cursor(cursor: OrdersCursor, *, filters_fp: str) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "t": cursor.created_at.isoformat(),
        "id": str(cursor.id),
        "f": filters_fp,
    }
    return base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode().rstrip("=")


def decode_cursor(raw: str, *, filters_fp: str) -> OrdersCursor:
    """
    Клиент может раскодировать курсор и подставить любые t/id.
    В нашем случае это безопасно: курсор лишь задаёт позицию внутри данных того же клиента.
    Подпись (HMAC) здесь не нужна, потому что фильтр customer_id берётся из параметра запроса,
    а не из курсора. Подделанный курсор может сдвинуть позицию только внутри заказов того же клиента.
    """
    try:
        payload = _OrdersCursorPayload.model_validate_json(
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

    return OrdersCursor(created_at=payload.t, id=payload.id)


# см. get_page_limit_from_query
async def decode_cursor_from_query_dep(
    customer_id: Annotated[UUID, Query()],
    cursor: Annotated[str | None, Query(max_length=CURSOR_MAX_LENGTH)] = None,
) -> OrdersCursor | None:
    if cursor is not None:
        return decode_cursor(cursor, filters_fp=filters_fingerprint({"customer_id": str(customer_id)}))
    return None


PageLimitFromQuery = Annotated[int, Depends(get_page_limit_from_query)]
OrdersCursorFromQuery = Annotated[OrdersCursor | None, Depends(decode_cursor_from_query_dep)]
