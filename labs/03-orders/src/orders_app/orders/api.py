import re
import uuid
from http import HTTPStatus
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, Depends, Header, Request, Response, status

from orders_app.core.errors import (
    VALIDATION_ERROR_SLUG,
    IdempotencyKeyInProgressError,
    IdempotencyKeyRequiredError,
    IdempotencyKeyReuseError,
    InvalidCursorError,
    NotFoundError,
)
from orders_app.core.middlewares.body_limit import CONTENT_TOO_LARGE_SLUG
from orders_app.core.openapi import (
    COMMON_PROBLEM_RESPONSES,
    EXAMPLE_ORDER_ID,
    RETRY_AFTER_HEADER,
    VALIDATION_EXAMPLE,
    error_example,
    problem_example,
    problem_response,
)
from orders_app.core.pagination import PageLimitFromQuery
from orders_app.db import DBConn
from orders_app.orders import services as order_service
from orders_app.orders.dependencies import CustomerIdFromQuery, OrdersCursorFromQuery
from orders_app.orders.schemas import CreateOrderRequest, OrderResponse, PaginatedOrderResponse

if TYPE_CHECKING:
    from orders_app.settings import OrdersSettings

IDEMPOTENCY_KEY_PATTERN = re.compile(r"[A-Za-z0-9_-]{8,64}")

# Общие ошибки (422, 500, 503) описаны на уровне роутера, у маршрутов — только свои
router = APIRouter(prefix="/v1/orders", tags=["Orders"], responses=COMMON_PROBLEM_RESPONSES)


@router.get(
    "/{order_id}",
    status_code=status.HTTP_200_OK,
    response_model=OrderResponse,
    responses={
        status.HTTP_404_NOT_FOUND: problem_response(
            "Order not found",
            {
                NotFoundError.slug: error_example(
                    NotFoundError,
                    instance=f"/v1/orders/{EXAMPLE_ORDER_ID}",
                    detail=f"Order {EXAMPLE_ORDER_ID} not found",
                ),
            },
        ),
    },
)
async def get_order(order_id: uuid.UUID, conn: DBConn) -> OrderResponse:
    return await order_service.get_order_by_id(conn, order_id=order_id)


@router.get(
    "",
    status_code=status.HTTP_200_OK,
    response_model=PaginatedOrderResponse,
    responses={
        status.HTTP_400_BAD_REQUEST: problem_response(
            "The cursor is malformed, has an unknown version or was issued for another customer_id",
            {
                "malformed": error_example(
                    InvalidCursorError,
                    instance="/v1/orders",
                    detail="Malformed cursor",
                ),
                "other-filters": error_example(
                    InvalidCursorError,
                    instance="/v1/orders",
                    detail="Cursor was issued for different filters",
                ),
            },
        ),
    },
)
async def get_orders(
    conn: DBConn,
    customer_id: CustomerIdFromQuery,
    limit: PageLimitFromQuery,
    cursor: OrdersCursorFromQuery,
) -> PaginatedOrderResponse:
    """
    Заказы клиента, новые сверху.
    ``customer_id`` обязателен, без него эндпоинт позволял бы выгрузить всю таблицу.
    """
    return await order_service.get_paginated_orders(
        conn,
        customer_id=customer_id,
        limit=limit,
        cursor=cursor,
    )


# async def, чтобы FastAPI не гонял проверку заголовка через пул потоков (см. core/pagination.py)
async def _get_idempotency_key(
    # pattern= здесь не ставим, потому что неверный формат проверял бы FastAPI и давал 422 вместо 400 по контракту.
    # По той же причине заголовок в схеме необязательный, его отсутствие мы обрабатываем сами.
    idempotency_key: str | None = Header(
        default=None,
        description="Required. Unique key of the operation, ^[A-Za-z0-9_-]{8,64}$. "
        "Missing or malformed key → 400 idempotency-key-required. "
        "The same key with the same body replays the stored response.",
    ),
) -> str:
    if not idempotency_key:
        raise IdempotencyKeyRequiredError(detail="Idempotency-Key header is required")
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(idempotency_key):
        raise IdempotencyKeyRequiredError(
            detail="Incorrect Idempotency-Key header. Correct template: ^[A-Za-z0-9_-]{8,64}$",
        )
    return idempotency_key


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=OrderResponse,
    responses={
        status.HTTP_201_CREATED: {
            "description": "Order created, or the stored response replayed for a repeated Idempotency-Key",
            "headers": {
                "Location": {"description": "Path of the created order", "schema": {"type": "string"}},
                "Idempotent-Replayed": {
                    "description": "`true` when this is the stored response of an earlier request with the same key",
                    "schema": {"type": "string", "enum": ["true"]},
                },
            },
        },
        status.HTTP_400_BAD_REQUEST: problem_response(
            "Idempotency-Key header is missing or does not match ^[A-Za-z0-9_-]{8,64}$",
            {
                "missing": error_example(
                    IdempotencyKeyRequiredError,
                    instance="/v1/orders",
                    detail="Idempotency-Key header is required",
                ),
                "bad-format": error_example(
                    IdempotencyKeyRequiredError,
                    instance="/v1/orders",
                    detail="Incorrect Idempotency-Key header. Correct template: ^[A-Za-z0-9_-]{8,64}$",
                ),
            },
        ),
        status.HTTP_409_CONFLICT: problem_response(
            "The original request with this Idempotency-Key is still running, retry after Retry-After seconds",
            {
                IdempotencyKeyInProgressError.slug: error_example(
                    IdempotencyKeyInProgressError,
                    instance="/v1/orders",
                    detail="The original request with this Idempotency-Key has not finished yet, retry later",
                ),
            },
            headers=RETRY_AFTER_HEADER,
        ),
        status.HTTP_413_CONTENT_TOO_LARGE: problem_response(
            "Request body exceeds max_request_body_bytes",
            {
                CONTENT_TOO_LARGE_SLUG: problem_example(
                    CONTENT_TOO_LARGE_SLUG,
                    HTTPStatus.CONTENT_TOO_LARGE.phrase,
                    HTTPStatus.CONTENT_TOO_LARGE,
                    instance="/v1/orders",
                    detail="Request body must not exceed 65536 bytes",
                ),
            },
        ),
        # 422 у POST — две разные ошибки, поэтому переопределяем общий ответ роутера с двумя примерами
        status.HTTP_422_UNPROCESSABLE_CONTENT: problem_response(
            "Request validation failed, or the Idempotency-Key was already used with a different body",
            {
                VALIDATION_ERROR_SLUG: VALIDATION_EXAMPLE,
                IdempotencyKeyReuseError.slug: error_example(
                    IdempotencyKeyReuseError,
                    instance="/v1/orders",
                    detail="This Idempotency-Key was used with a different request body",
                ),
            },
        ),
    },
)
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
