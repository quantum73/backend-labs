import hashlib
import json
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy
import sqlalchemy.exc
from sqlalchemy import RowMapping, select, tuple_
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from orders_app.core.enums import IdempotencyKeyScope, OrderStatus
from orders_app.core.errors import IdempotencyKeyInProgressError, IdempotencyKeyReuseError, NotFoundError
from orders_app.core.pagination import Cursor, encode_cursor
from orders_app.db import idempotency_keys, order_items, orders
from orders_app.orders.pagination import orders_filters_fp
from orders_app.orders.schemas import CreateOrderRequest, OrderItemOut, OrderResponse, PaginatedOrderResponse

LOCK_NOT_AVAILABLE = "55P03"


def request_fingerprint(body: CreateOrderRequest) -> str:
    """Хешируем ВАЛИДИРОВАННУЮ модель в каноничном виде. Порядок ключей и пробелы в JSON не важны."""
    canonical = json.dumps(body.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


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
    cursor: Cursor | None,
) -> PaginatedOrderResponse:
    next_cursor = None

    query = select(orders).where(orders.c.customer_id == customer_id)
    if cursor is not None:
        query = query.where(
            tuple_(orders.c.created_at, orders.c.id) < tuple_(cursor.created_at, cursor.id),
        )
    query = query.order_by(orders.c.created_at.desc(), orders.c.id.desc()).limit(limit + 1)

    order_rows = (await conn.execute(query)).mappings().all()
    has_more = len(order_rows) > limit
    order_rows = order_rows[:limit]
    if has_more:
        last_row = order_rows[-1]
        next_cursor = encode_cursor(
            cursor=Cursor(created_at=last_row.created_at, id=last_row.id),
            filters_fp=orders_filters_fp(customer_id),
        )

    items_by_order = await _get_order_items(conn, order_ids=[row["id"] for row in order_rows])
    return PaginatedOrderResponse(
        items=[_to_order_response(row, items_by_order[row["id"]]) for row in order_rows],
        next_cursor=next_cursor,
    )


async def get_order_by_id(conn: AsyncConnection, *, order_id: uuid.UUID) -> OrderResponse:
    query = select(orders).where(orders.c.id == order_id)
    order_row = (await conn.execute(query)).mappings().one_or_none()
    if order_row is None:
        raise NotFoundError(detail=f"Order {order_id} not found")

    items_by_order = await _get_order_items(conn, order_ids=[order_id])
    return _to_order_response(order_row, items_by_order[order_id])


@dataclass(frozen=True, slots=True)
class CreateResult:
    order: OrderResponse
    replayed: bool


async def idempotent_create_order(
    conn: AsyncConnection,
    *,
    create_order_payload: CreateOrderRequest,
    idempotency_key: str,
    idempotency_ttl_s: int,
) -> CreateResult:
    """
    Вопрос №1:
    Почему ключ, заказ и сохранённый ответ должны быть в одной транзакции? Что сломается при двух отдельных транзакциях?
    Ответ:
    При двух отдельных транзакциях получаем недопустимые побочные эффекты:
    - Ключ закоммичен, а заказ не создан (падение между транзакциями).
    Ключ занят без ответа: повторы вечно получают «в обработке» или, при наивной логике, создают заказ без защиты;
    - Заказ закоммичен, а ключ или ответ не записан. Повтор не находит ключ и создаёт второй заказ — двойное списание;
    - Блокировка: строка, вставленная по уникальному индексу, держит параллельные дубли до коммита всей операции.
      В одной транзакции они ждут и получают готовый ответ.
      При отдельной транзакции, для ключа блокировка снимается раньше, чем появится ответ.
    Поэтому нужно выполнять всё в одной транзакции.

    Вопрос №2:
    Что изменилось бы, если бы создание заказа включало вызов внешнего платёжного API?
    Ответ:
    В таком случае нужно:
    - Занять ключ со статусом in_progress и создать заказ в одной транзакции (Т1);
    - Выполнить вызов платёжного шлюза вне транзакции, передав ему тот же ключ идемпотентности
    (Провайдер должен поддерживать идемпотентность сам).
    - Отдельной транзакцией (Т2) записать итоговый результат.
    При повторном запросе с тем же ключом идемпотентности нужно отвечать 409 статусом с хэдером Retry-After.
    Но при этом нужно держать заказ в статусе in_progress определённое время, так как шлюз может отвалиться.
    В таком случае нужна, например, reconciliation джоба, которая будет переводить висящие заказы в статус suspended.

    Примечание:
    В идеале нужна периодическая очистка истёкших ключей (DELETE ... WHERE expires_at < now())
    и индекс по expires_at (его я добавил), но для лабы это можно опустить.
    """

    now = datetime.now(UTC)
    fingerprint = request_fingerprint(create_order_payload)
    claim = (
        insert(idempotency_keys)
        .values(
            scope=IdempotencyKeyScope.CREATE_ORDER,
            key=idempotency_key,
            request_hash=fingerprint,
            response_status=sqlalchemy.null(),
            response_body=sqlalchemy.null(),
            created_at=now,
            expires_at=now + timedelta(seconds=idempotency_ttl_s),
        )
        .on_conflict_do_update(
            index_elements=["scope", "key"],
            set_={
                "request_hash": fingerprint,
                "created_at": now,
                "expires_at": now + timedelta(seconds=idempotency_ttl_s),
                "response_status": sqlalchemy.null(),
                "response_body": sqlalchemy.null(),
            },
            where=idempotency_keys.c.expires_at < now,
        )
        .returning(idempotency_keys.c.key)
    )
    # Если этот ключ сейчас вставляет другая транзакция, INSERT ждёт её COMMIT/ROLLBACK, но не дольше lock_timeout
    try:
        claimed = (await conn.execute(claim)).first()
    except sqlalchemy.exc.DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == LOCK_NOT_AVAILABLE:
            raise IdempotencyKeyInProgressError(
                detail="The original request with this Idempotency-Key has not finished yet, retry later",
            ) from None
        raise

    if claimed is None:
        # response_status не читаем. Единственная операция под ключом всегда отвечает 201.
        # Он пригодится, когда под ключом появятся операции с другими статусами ответа.
        stored = (
            await conn.execute(
                select(idempotency_keys.c.request_hash, idempotency_keys.c.response_body).where(
                    idempotency_keys.c.scope == IdempotencyKeyScope.CREATE_ORDER,
                    idempotency_keys.c.key == idempotency_key,
                )
            )
        ).one()
        if stored.request_hash != fingerprint:
            raise IdempotencyKeyReuseError("This Idempotency-Key was used with a different request body")

        return CreateResult(order=OrderResponse.model_validate(stored.response_body), replayed=True)

    order = await create_order(conn, create_order_payload=create_order_payload, created_at=now)
    await conn.execute(  # ответ сохраняем в ТОЙ ЖЕ транзакции
        idempotency_keys.update()
        .where(
            idempotency_keys.c.scope == IdempotencyKeyScope.CREATE_ORDER,
            idempotency_keys.c.key == idempotency_key,
        )
        .values(response_status=201, response_body=order.model_dump(mode="json"))
    )
    return CreateResult(order=order, replayed=False)


async def create_order(
    conn: AsyncConnection,
    *,
    create_order_payload: CreateOrderRequest,
    created_at: datetime,
) -> OrderResponse:
    order_values = {
        "id": uuid.uuid7(),
        "customer_id": create_order_payload.customer_id,
        "status": OrderStatus.CREATED,
        "currency": create_order_payload.currency,
        "total_kopecks": sum(item.unit_price_kopecks * item.quantity for item in create_order_payload.items),
        "created_at": created_at,
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
