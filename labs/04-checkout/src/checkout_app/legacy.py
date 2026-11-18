"""
Так сервис оформления заказов выглядит сегодня. Он работает и приносит деньги, но каждое изменение в нём
страшно делать: правила цен, SQL, HTTP и вызов внешнего сервиса перемешаны в одном обработчике.

В лабе этот файл не улучшают по кусочку, а заменяют слоями под защитой характеризационных тестов.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import httpx
from fastapi import APIRouter, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from sqlalchemy import (
    BigInteger,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

metadata = MetaData()

products = Table(
    "products",
    metadata,
    Column("sku", Text, primary_key=True),
    Column("name", Text, nullable=False),
    Column("price_kopecks", BigInteger, nullable=False),
    Column("stock", Integer, nullable=False),
)

promo_codes = Table(
    "promo_codes",
    metadata,
    Column("code", Text, primary_key=True),
    Column("percent_off", Integer, nullable=False),
    Column("min_subtotal_kopecks", BigInteger, nullable=False),
    Column("max_discount_kopecks", BigInteger, nullable=False),
    Column("valid_until", DateTime(timezone=True), nullable=False),
    Column("max_uses", Integer, nullable=False),
    Column("used_count", Integer, nullable=False, server_default="0"),
)

promo_redemptions = Table(
    "promo_redemptions",
    metadata,
    Column("code", Text, ForeignKey("promo_codes.code"), primary_key=True),
    Column("customer_id", Uuid, primary_key=True),
    Column("order_id", Uuid, nullable=False),
)

orders = Table(
    "orders",
    metadata,
    Column("id", Uuid, primary_key=True),
    Column("customer_id", Uuid, nullable=False),
    Column("email", Text, nullable=False),
    Column("status", Text, nullable=False),
    Column("subtotal_kopecks", BigInteger, nullable=False),
    Column("discount_kopecks", BigInteger, nullable=False),
    Column("delivery_fee_kopecks", BigInteger, nullable=False),
    Column("total_kopecks", BigInteger, nullable=False),
    Column("promo_code", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
)

order_lines = Table(
    "order_lines",
    metadata,
    Column("order_id", Uuid, ForeignKey("orders.id"), primary_key=True),
    Column("line_no", Integer, primary_key=True),
    Column("sku", Text, nullable=False),
    Column("quantity", Integer, nullable=False),
    Column("unit_price_kopecks", BigInteger, nullable=False),
)

TYPE_BASE = "https://errors.checkout.example/"


def problem(status: int, slug: str, title: str, detail: str | None = None, **extensions: Any) -> JSONResponse:
    body: dict[str, Any] = {"type": TYPE_BASE + slug, "title": title, "status": status}
    if detail:
        body["detail"] = detail
    return JSONResponse(body | extensions, status_code=status, media_type="application/problem+json")


async def validation_error_handler(_: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)  # noqa: S101
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return problem(422, "validation-error", "Request validation failed", errors=errors)


class CheckoutItem(BaseModel):
    sku: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    quantity: int = Field(ge=1, le=10)


class CheckoutRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: uuid.UUID
    email: Annotated[str, StringConstraints(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=254)]
    items: list[CheckoutItem] = Field(min_length=1, max_length=20)
    promo_code: str | None = None


router = APIRouter()


async def _load_order(conn: AsyncConnection, order_id: uuid.UUID) -> dict[str, Any] | None:
    order = (await conn.execute(text("SELECT * FROM orders WHERE id = :id"), {"id": order_id})).mappings().first()
    if order is None:
        return None
    lines = (
        (
            await conn.execute(
                text("SELECT * FROM order_lines WHERE order_id = :id ORDER BY line_no"),
                {"id": order_id},
            )
        )
        .mappings()
        .all()
    )
    return {
        "id": str(order["id"]),
        "customer_id": str(order["customer_id"]),
        "email": order["email"],
        "status": order["status"],
        "lines": [
            {
                "sku": line["sku"],
                "quantity": line["quantity"],
                "unit_price_kopecks": line["unit_price_kopecks"],
                "line_total_kopecks": line["unit_price_kopecks"] * line["quantity"],
            }
            for line in lines
        ],
        "subtotal_kopecks": order["subtotal_kopecks"],
        "discount_kopecks": order["discount_kopecks"],
        "delivery_fee_kopecks": order["delivery_fee_kopecks"],
        "total_kopecks": order["total_kopecks"],
        "promo_code": order["promo_code"],
        "created_at": order["created_at"].isoformat(),
        "cancelled_at": order["cancelled_at"].isoformat() if order["cancelled_at"] else None,
    }


@router.post("/v1/checkout")
async def checkout(request: Request, body: CheckoutRequest) -> JSONResponse:
    engine: AsyncEngine = request.app.state.engine
    notifier: httpx.AsyncClient = request.app.state.notifier

    # одинаковые SKU объединяем в одну строку
    merged: dict[str, int] = {}
    for item in body.items:
        merged[item.sku] = merged.get(item.sku, 0) + item.quantity
    for sku, qty in merged.items():
        if qty > 10:
            return problem(422, "quantity-limit", "Too many units of one product", f"{sku}: {qty} > 10")

    async with engine.begin() as conn:
        rows = (
            (
                await conn.execute(
                    text("SELECT sku, price_kopecks, stock FROM products WHERE sku = ANY(:skus)"),
                    {"skus": list(merged)},
                )
            )
            .mappings()
            .all()
        )
        found = {row["sku"]: row for row in rows}
        missing = [sku for sku in merged if sku not in found]
        if missing:
            return problem(422, "unknown-sku", "Unknown product", ", ".join(sorted(missing)))
        for sku, qty in merged.items():
            if found[sku]["stock"] < qty:
                return problem(
                    409,
                    "insufficient-stock",
                    "Not enough stock",
                    f"{sku}: requested {qty}, available {found[sku]['stock']}",
                )

        subtotal = 0
        for sku, qty in merged.items():
            subtotal += found[sku]["price_kopecks"] * qty

        discount = 0
        code = None
        if body.promo_code:
            code = body.promo_code.strip().upper()
            promo = (
                (await conn.execute(text("SELECT * FROM promo_codes WHERE code = :code"), {"code": code}))
                .mappings()
                .first()
            )
            if promo is None:
                return problem(422, "promo-not-found", "Promo code not found", code)
            if promo["valid_until"] <= datetime.now(UTC):
                return problem(422, "promo-expired", "Promo code expired", code)
            if subtotal < promo["min_subtotal_kopecks"]:
                return problem(
                    422,
                    "promo-min-subtotal",
                    "Order is too small for this promo code",
                    f"minimum {promo['min_subtotal_kopecks']} kopecks",
                )
            used = (
                await conn.execute(
                    text("SELECT 1 FROM promo_redemptions WHERE code = :code AND customer_id = :customer_id"),
                    {"code": code, "customer_id": body.customer_id},
                )
            ).first()
            if used:
                return problem(409, "promo-already-used", "Promo code already used by this customer", code)
            if promo["used_count"] >= promo["max_uses"]:
                return problem(409, "promo-exhausted", "Promo code usage limit reached", code)
            discount = subtotal * promo["percent_off"] // 100
            if discount > promo["max_discount_kopecks"]:
                discount = promo["max_discount_kopecks"]

        # бесплатная доставка от 3000 ₽ после скидки
        delivery_fee = 29900 if subtotal - discount < 300000 else 0
        total = subtotal - discount + delivery_fee

        for sku, qty in merged.items():
            await conn.execute(
                text("UPDATE products SET stock = :stock WHERE sku = :sku"),
                {"stock": found[sku]["stock"] - qty, "sku": sku},
            )

        order_id = uuid.uuid4()
        await conn.execute(
            text(
                "INSERT INTO orders (id, customer_id, email, status, subtotal_kopecks, discount_kopecks, "
                "delivery_fee_kopecks, total_kopecks, promo_code, created_at) "
                "VALUES (:id, :customer_id, :email, 'created', :subtotal, :discount, :delivery_fee, :total, "
                ":promo_code, :created_at)"
            ),
            {
                "id": order_id,
                "customer_id": body.customer_id,
                "email": body.email,
                "subtotal": subtotal,
                "discount": discount,
                "delivery_fee": delivery_fee,
                "total": total,
                "promo_code": code,
                "created_at": datetime.now(UTC),
            },
        )
        for line_no, (sku, qty) in enumerate(merged.items(), start=1):
            await conn.execute(
                text(
                    "INSERT INTO order_lines (order_id, line_no, sku, quantity, unit_price_kopecks) "
                    "VALUES (:order_id, :line_no, :sku, :quantity, :price)"
                ),
                {
                    "order_id": order_id,
                    "line_no": line_no,
                    "sku": sku,
                    "quantity": qty,
                    "price": found[sku]["price_kopecks"],
                },
            )
        if code:
            await conn.execute(
                text("UPDATE promo_codes SET used_count = used_count + 1 WHERE code = :code"), {"code": code}
            )
            await conn.execute(
                text(
                    "INSERT INTO promo_redemptions (code, customer_id, order_id) "
                    "VALUES (:code, :customer_id, :order_id)"
                ),
                {"code": code, "customer_id": body.customer_id, "order_id": order_id},
            )

        # письмо клиенту
        response = await notifier.post(
            "/v1/notifications",
            json={"template": "order_created", "email": body.email, "order_id": str(order_id), "total_kopecks": total},
        )
        response.raise_for_status()

        order = await _load_order(conn, order_id)

    return JSONResponse(order, status_code=201, headers={"Location": f"/v1/orders/{order_id}"})


@router.get("/v1/orders/{order_id}")
async def get_order(request: Request, order_id: uuid.UUID) -> JSONResponse:
    engine: AsyncEngine = request.app.state.engine
    async with engine.connect() as conn:
        order = await _load_order(conn, order_id)
    if order is None:
        return problem(404, "order-not-found", "Order not found", str(order_id))
    return JSONResponse(order)


@router.post("/v1/orders/{order_id}/cancel")
async def cancel_order(request: Request, order_id: uuid.UUID) -> JSONResponse:
    engine: AsyncEngine = request.app.state.engine
    async with engine.begin() as conn:
        order = (await conn.execute(text("SELECT * FROM orders WHERE id = :id"), {"id": order_id})).mappings().first()
        if order is None:
            return problem(404, "order-not-found", "Order not found", str(order_id))
        if order["status"] != "created":
            return problem(409, "order-not-cancellable", "Order cannot be cancelled", "Order is already cancelled")
        if datetime.now(UTC) - order["created_at"] > timedelta(minutes=30):
            return problem(
                409,
                "order-not-cancellable",
                "Order cannot be cancelled",
                "Cancellation window of 30 minutes has passed",
            )

        lines = (
            (await conn.execute(text("SELECT sku, quantity FROM order_lines WHERE order_id = :id"), {"id": order_id}))
            .mappings()
            .all()
        )
        for line in lines:
            await conn.execute(
                text("UPDATE products SET stock = stock + :quantity WHERE sku = :sku"),
                {"quantity": line["quantity"], "sku": line["sku"]},
            )
        if order["promo_code"]:
            await conn.execute(
                text("UPDATE promo_codes SET used_count = used_count - 1 WHERE code = :code"),
                {"code": order["promo_code"]},
            )
            await conn.execute(
                text("DELETE FROM promo_redemptions WHERE code = :code AND customer_id = :customer_id"),
                {"code": order["promo_code"], "customer_id": order["customer_id"]},
            )
        await conn.execute(
            text("UPDATE orders SET status = 'cancelled', cancelled_at = :now WHERE id = :id"),
            {"now": datetime.now(UTC), "id": order_id},
        )
        result = await _load_order(conn, order_id)

    return JSONResponse(result)
