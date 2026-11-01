import asyncio
import time
from collections.abc import AsyncIterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from orders_app.orders.schemas import IdempotencyKeyScope
from orders_app.settings import OrdersSettings
from tests.acceptance.helpers import assert_problem, create, new_key, order_body

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def db_engine(settings: OrdersSettings) -> AsyncIterator[AsyncEngine]:
    """Отдельный engine, чтобы смотреть в БД мимо API."""
    engine = create_async_engine(settings.database_url.get_secret_value())
    try:
        yield engine
    finally:
        await engine.dispose()


async def _row_counts(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as conn:
        result = await conn.execute(
            text("SELECT (SELECT count(*) FROM orders), (SELECT count(*) FROM order_items)"),
        )
        orders_count, items_count = result.one()
    return orders_count, items_count


async def _expire_key(engine: AsyncEngine, key: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("UPDATE idempotency_keys SET expires_at = now() - interval '1 second' WHERE key = :key"),
            {"key": key},
        )


async def test_replay_does_not_change_database(client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    body, key = order_body(), new_key()
    first = await create(client, body, key)
    assert first.status_code == 201, first.text
    counts_before = await _row_counts(db_engine)

    sequential = await create(client, body, key)
    concurrent = await asyncio.gather(*(create(client, body, key) for _ in range(5)))

    assert await _row_counts(db_engine) == counts_before
    for replay in [sequential, *concurrent]:
        assert replay.status_code == 201
        assert replay.headers["idempotent-replayed"] == "true"
        assert replay.json() == first.json()


async def test_replay_waiting_for_unfinished_original_is_409(
    client_factory,
    settings: OrdersSettings,
    db_engine: AsyncEngine,
    capsys,
) -> None:
    lock_timeout_s = 0.3
    app_settings = OrdersSettings.model_validate(
        {
            **settings.model_dump(),
            "db_lock_timeout_ms": int(lock_timeout_s * 1000),
        }
    )
    body, key = order_body(), new_key()

    async with client_factory(app_settings=app_settings) as client:
        # Оригинал «ещё выполняется»: claim вставлен, транзакция не закоммичена
        async with db_engine.connect() as original:
            await original.begin()
            await original.execute(
                text(
                    "INSERT INTO idempotency_keys (scope, key, request_hash, created_at, expires_at) "
                    "VALUES (:scope, :key, 'in-flight', now(), now() + interval '1 day')"
                ),
                {"scope": IdempotencyKeyScope.CREATE_ORDER, "key": key},
            )
            started = time.perf_counter()
            replay = await create(client, body, key)
            elapsed = time.perf_counter() - started
            await original.rollback()

        # Оригинал не дошёл до коммита: ключ свободен, и тот же запрос создаёт заказ
        retry = await create(client, body, key)

    assert_problem(replay, 409, "idempotency-key-in-progress")
    assert replay.headers["retry-after"] == "1"
    # Повтор действительно ждал блокировку, но ответил по lock_timeout, а не по statement_timeout
    assert lock_timeout_s * 0.9 <= elapsed < settings.db_statement_timeout_ms / 1000, f"ответ через {elapsed:.2f}s"
    assert "unhandled_error" not in capsys.readouterr().out
    assert retry.status_code == 201, retry.text
    assert "idempotent-replayed" not in retry.headers


async def test_expired_key_can_be_reused(client: httpx.AsyncClient, db_engine: AsyncEngine) -> None:
    key = new_key()
    first = await create(client, order_body(), key)
    assert first.status_code == 201, first.text
    await _expire_key(db_engine, key)

    reused = await create(client, order_body(), key)  # другой customer_id для живого ключа это 422
    assert reused.status_code == 201, reused.text
    assert reused.json()["id"] != first.json()["id"]
    assert "idempotent-replayed" not in reused.headers


async def test_live_key_with_other_body_is_still_422(client: httpx.AsyncClient) -> None:
    key = new_key()
    assert (await create(client, order_body(), key)).status_code == 201
    assert_problem(await create(client, order_body(), key), 422, "idempotency-key-reuse")
