import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request, Response, status

from orders_app.db import DBConn
from orders_app.orders import services as order_service
from orders_app.orders.schemas import CreateOrderRequest, OrderResponse, PaginatedOrderResponse
from orders_app.pagination import PageLimitFromQuery

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


@router.post("", status_code=status.HTTP_201_CREATED, response_model=OrderResponse)
async def create_order(
    request: Request,
    response: Response,
    create_order_request: CreateOrderRequest,
    conn: DBConn,
) -> OrderResponse:
    order = await order_service.create_order(conn, create_order_payload=create_order_request)
    response.headers["Location"] = request.app.url_path_for("get_order", order_id=order.id)
    return order
