import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime

from sqlalchemy import RowMapping, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection

from orders_app.db import order_items, orders
from orders_app.errors import NotFoundError
from orders_app.orders.schemas import (
    CreateOrderRequest,
    OrderItemOut,
    OrderResponse,
    OrderStatus,
    PaginatedOrderResponse,
)


async def _get_order_items(
    conn: AsyncConnection,
    *,
    order_ids: Sequence[uuid.UUID],
) -> defaultdict[uuid.UUID, list[OrderItemOut]]:
    """Позиции всех заказов одним запросом"""
    items_by_order: defaultdict[uuid.UUID, list[OrderItemOut]] = defaultdict(list)
    if not order_ids:
        return items_by_order

    query = (
        select(
            order_items.c.order_id,
            order_items.c.sku,
            order_items.c.quantity,
            order_items.c.unit_price_kopecks,
        )
        .where(order_items.c.order_id.in_(order_ids))
        .order_by(order_items.c.order_id, order_items.c.line_no)
    )
    for row in (await conn.execute(query)).mappings():
        items_by_order[row["order_id"]].append(OrderItemOut.model_validate(row))

    return items_by_order


def _to_order_response(order_row: RowMapping, items: list[OrderItemOut]) -> OrderResponse:
    return OrderResponse.model_validate({**order_row, "items": items})


async def get_paginated_orders(
    conn: AsyncConnection,
    *,
    customer_id: uuid.UUID,
    limit: int,
) -> PaginatedOrderResponse:
    query = (
        select(orders)
        .where(orders.c.customer_id == customer_id)
        .order_by(orders.c.created_at.desc(), orders.c.id.desc())
        .limit(limit)
    )
    order_rows = (await conn.execute(query)).mappings().all()
    items_by_order = await _get_order_items(conn, order_ids=[row["id"] for row in order_rows])
    return PaginatedOrderResponse(
        items=[_to_order_response(row, items_by_order[row["id"]]) for row in order_rows],
        next_cursor=None,
    )


async def get_order_by_id(conn: AsyncConnection, *, order_id: uuid.UUID) -> OrderResponse:
    query = select(orders).where(orders.c.id == order_id)
    order_row = (await conn.execute(query)).mappings().one_or_none()
    if order_row is None:
        raise NotFoundError(detail=f"Order {order_id} not found")

    items_by_order = await _get_order_items(conn, order_ids=[order_id])
    return _to_order_response(order_row, items_by_order[order_id])


async def create_order(conn: AsyncConnection, *, create_order_payload: CreateOrderRequest) -> OrderResponse:
    order_values = {
        "id": uuid.uuid7(),
        "customer_id": create_order_payload.customer_id,
        "status": OrderStatus.CREATED,
        "currency": create_order_payload.currency,
        "total_kopecks": sum(item.unit_price_kopecks * item.quantity for item in create_order_payload.items),
        "created_at": datetime.now(UTC),
    }
    item_values = [
        {
            "order_id": order_values["id"],
            "line_no": line_no,
            "sku": item.sku,
            "quantity": item.quantity,
            "unit_price_kopecks": item.unit_price_kopecks,
        }
        for line_no, item in enumerate(create_order_payload.items, start=1)
    ]
    await conn.execute(insert(orders).values(order_values))
    await conn.execute(insert(order_items), item_values)

    items = [OrderItemOut.model_validate(item, from_attributes=True) for item in create_order_payload.items]
    return OrderResponse.model_validate({**order_values, "items": items})
