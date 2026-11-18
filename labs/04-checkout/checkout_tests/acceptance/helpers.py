import asyncio
import json
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "https://errors.checkout.example/"


@dataclass
class FakeNotifier:
    """
    Сервис уведомлений для тестов.
    Запоминает каждый вызов и проверяет, виден ли заказ в БД из другого соединения,
    то есть закоммичен ли он к моменту отправки письма.
    """

    engine: AsyncEngine
    mode: Literal["ok", "fail", "hang"] = "ok"
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def handle(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "payload": payload,
                "order_committed": await self._order_committed(payload.get("order_id")),
            }
        )
        if self.mode == "fail":
            return httpx.Response(503, json={"error": "unavailable"})
        if self.mode == "hang":
            await asyncio.sleep(30)
        return httpx.Response(202, json={"status": "queued"})

    async def _order_committed(self, order_id: str | None) -> bool:
        if order_id is None:
            return False
        async with self.engine.connect() as conn:
            row = await conn.execute(text("SELECT 1 FROM orders WHERE id = :id"), {"id": uuid.UUID(order_id)})
            return row.first() is not None


@dataclass
class Seed:
    """Наполнение БД и проверки мимо API. У каждого теста свои SKU и промокоды: тесты не мешают друг другу."""

    engine: AsyncEngine

    async def product(self, *, price_kopecks: int = 100_000, stock: int = 10, name: str = "Товар") -> str:
        sku = f"SKU-{uuid.uuid4().hex[:12].upper()}"
        async with self.engine.begin() as conn:
            await conn.execute(
                text("INSERT INTO products (sku, name, price_kopecks, stock) VALUES (:sku, :name, :price, :stock)"),
                {"sku": sku, "name": name, "price": price_kopecks, "stock": stock},
            )
        return sku

    async def promo(
        self,
        *,
        percent_off: int = 10,
        min_subtotal_kopecks: int = 0,
        max_discount_kopecks: int = 10_000_000,
        valid_for: timedelta = timedelta(days=7),
        max_uses: int = 100,
        used_count: int = 0,
    ) -> str:
        code = f"PROMO{uuid.uuid4().hex[:8].upper()}"
        async with self.engine.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO promo_codes (code, percent_off, min_subtotal_kopecks, max_discount_kopecks, "
                    "valid_until, max_uses, used_count) "
                    "VALUES (:code, :percent_off, :min_subtotal, :max_discount, :valid_until, :max_uses, :used_count)"
                ),
                {
                    "code": code,
                    "percent_off": percent_off,
                    "min_subtotal": min_subtotal_kopecks,
                    "max_discount": max_discount_kopecks,
                    "valid_until": datetime.now(UTC) + valid_for,
                    "max_uses": max_uses,
                    "used_count": used_count,
                },
            )
        return code

    async def stock(self, sku: str) -> int:
        async with self.engine.connect() as conn:
            value = await conn.scalar(text("SELECT stock FROM products WHERE sku = :sku"), {"sku": sku})
        assert isinstance(value, int)
        return value

    async def promo_used_count(self, code: str) -> int:
        async with self.engine.connect() as conn:
            value = await conn.scalar(text("SELECT used_count FROM promo_codes WHERE code = :code"), {"code": code})
        assert isinstance(value, int)
        return value

    async def redemptions(self, code: str) -> int:
        async with self.engine.connect() as conn:
            value = await conn.scalar(text("SELECT count(*) FROM promo_redemptions WHERE code = :code"), {"code": code})
        assert isinstance(value, int)
        return value

    async def orders_of(self, customer_id: str) -> int:
        async with self.engine.connect() as conn:
            value = await conn.scalar(
                text("SELECT count(*) FROM orders WHERE customer_id = :customer_id"),
                {"customer_id": uuid.UUID(customer_id)},
            )
        assert isinstance(value, int)
        return value

    async def age_order(self, order_id: str, *, minutes: int) -> None:
        """Сдвигает created_at заказа в прошлое: так проверяется окно отмены без управления часами приложения."""
        async with self.engine.begin() as conn:
            await conn.execute(
                text("UPDATE orders SET created_at = now() - make_interval(mins => :minutes) WHERE id = :id"),
                {"minutes": minutes, "id": uuid.UUID(order_id)},
            )


def checkout_body(
    items: list[tuple[str, int]],
    *,
    customer_id: str | None = None,
    email: str = "buyer@example.com",
    promo_code: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "customer_id": customer_id or str(uuid.uuid4()),
        "email": email,
        "items": [{"sku": sku, "quantity": quantity} for sku, quantity in items],
    }
    if promo_code is not None:
        body["promo_code"] = promo_code
    return body


async def checkout(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/v1/checkout", json=body)


def assert_problem(response: httpx.Response, status: int, slug: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith(PROBLEM_JSON), response.headers["content-type"]
    problem: dict[str, Any] = response.json()
    assert problem["status"] == status
    assert problem["title"]
    assert problem["type"] == TYPE_BASE + slug, problem
    return problem
