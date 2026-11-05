from typing import Annotated
from uuid import UUID

from fastapi import Depends, Query

from orders_app.core.pagination import Cursor, decode_cursor
from orders_app.orders.pagination import orders_filters_fp

# Текущий закодированный курсор занимает ~210 символов, 512 - значение с запасом.
ORDERS_CURSOR_MAX_LENGTH = 512

CustomerIdFromQuery = Annotated[
    UUID,
    Query(description="Обязательный фильтр: заказы только одного клиента."),
]


# см. core/pagination.py
async def decode_orders_cursor_from_query_dep(
    customer_id: CustomerIdFromQuery,
    cursor: Annotated[str | None, Query(max_length=ORDERS_CURSOR_MAX_LENGTH)] = None,
) -> Cursor | None:
    if cursor is not None:
        return decode_cursor(cursor, filters_fp=orders_filters_fp(customer_id))
    return None


OrdersCursorFromQuery = Annotated[Cursor | None, Depends(decode_orders_cursor_from_query_dep)]
