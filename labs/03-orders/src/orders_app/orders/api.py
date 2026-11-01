import re
import uuid
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status

from orders_app.db import DBConn
from orders_app.errors import IdempotencyKeyRequiredError
from orders_app.orders import services as order_service
from orders_app.orders.schemas import CreateOrderRequest, OrderResponse, PaginatedOrderResponse
from orders_app.pagination import PageLimitFromQuery

if TYPE_CHECKING:
    from orders_app.settings import OrdersSettings

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")

router = APIRouter(prefix="/v1/orders", tags=["Orders"])


@router.get(
    "/{order_id}",
    status_code=status.HTTP_200_OK,
    response_model=OrderResponse,
)
async def get_order(order_id: uuid.UUID, conn: DBConn) -> OrderResponse:
    return await order_service.get_order_by_id(conn, order_id=order_id)


@router.get("", status_code=status.HTTP_200_OK, response_model=PaginatedOrderResponse)
async def get_orders(
    conn: DBConn,
    customer_id: Annotated[
        uuid.UUID,
        Query(description="Обязательный фильтр: заказы только одного клиента."),
    ],
    limit: PageLimitFromQuery,
) -> PaginatedOrderResponse:
    """
    Заказы клиента, новые сверху.
    ``customer_id`` обязателен, без него эндпоинт позволял бы выгрузить всю таблицу.
    """
    return await order_service.get_paginated_orders(conn, customer_id=customer_id, limit=limit)


def _get_idempotency_key(idempotency_key: str | None = Header(default=None)) -> str:
    if not idempotency_key:
        raise IdempotencyKeyRequiredError(detail="Idempotency-Key header is required")
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise IdempotencyKeyRequiredError(
            detail="Incorrect Idempotency-Key header. Correct template: ^[A-Za-z0-9_-]{8,64}$",
        )
    return idempotency_key


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrderResponse)
async def create_order(
    request: Request,
    response: Response,
    create_order_request: CreateOrderRequest,
    idempotency_key: Annotated[str, Depends(_get_idempotency_key)],
    conn: DBConn,
) -> OrderResponse:
    settings: OrdersSettings = request.state.settings
    resp = await order_service.idempotent_create_order(
        conn,
        create_order_payload=create_order_request,
        idempotency_key=idempotency_key,
        idempotency_ttl_s=settings.idempotency_ttl_s,
    )
    response.headers["Location"] = request.app.url_path_for("get_order", order_id=resp.order.id)
    if resp.replayed:
        response.headers["Idempotent-Replayed"] = "true"
    return resp.order
