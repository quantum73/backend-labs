import json
import uuid
from collections.abc import AsyncIterator

import pytest
from starlette.types import Message, Receive, Scope, Send

from orders_app.core.middlewares.body_limit import BodySizeLimitMiddleware
from orders_app.settings import OrdersSettings
from tests.acceptance.helpers import assert_problem, create, new_key, order_body

pytestmark = pytest.mark.asyncio

MAX_BODY_BYTES = 1024


def _limited(settings: OrdersSettings) -> OrdersSettings:
    return OrdersSettings.model_validate({**settings.model_dump(), "max_request_body_bytes": MAX_BODY_BYTES})


def _oversized_body() -> bytes:
    return json.dumps(order_body(junk="x" * (MAX_BODY_BYTES * 2))).encode()


def _assert_too_large(response) -> None:
    problem = assert_problem(response, 413, "content-too-large")
    assert str(MAX_BODY_BYTES) in problem["detail"]
    assert response.headers["x-request-id"]  # 413 проходит через RequestContextMiddleware


async def test_body_with_content_length_over_limit_is_413(client_factory, settings: OrdersSettings) -> None:
    async with client_factory(app_settings=_limited(settings)) as client:
        response = await client.post(
            "/v1/orders",
            content=_oversized_body(),
            headers={"Idempotency-Key": new_key(), "Content-Type": "application/json"},
        )
    _assert_too_large(response)


async def test_content_length_over_limit_is_rejected_without_reading_body() -> None:
    # Проверка Content-Length позволяет не читать огромное тело в память вовсе
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise AssertionError("приложение не должно вызываться")

    async def receive() -> Message:
        raise AssertionError("тело не должно читаться")

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    scope: Scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/orders",
        "query_string": b"",
        "headers": [(b"content-length", str(MAX_BODY_BYTES + 1).encode())],
    }
    await BodySizeLimitMiddleware(app, max_body_bytes=MAX_BODY_BYTES)(scope, receive, send)

    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 413


async def test_chunked_body_over_limit_is_413(
    client_factory,
    settings: OrdersSettings,
) -> None:
    body = _oversized_body()

    async def chunks() -> AsyncIterator[bytes]:
        # Тело без Content-Length (chunked) - лимит срабатывает по мере чтения, а не по заголовку
        for start in range(0, len(body), 256):
            yield body[start : start + 256]

    async with client_factory(app_settings=_limited(settings)) as client:
        response = await client.post(
            "/v1/orders",
            content=chunks(),
            headers={"Idempotency-Key": new_key(), "Content-Type": "application/json"},
        )

    assert "content-length" not in response.request.headers
    _assert_too_large(response)


async def test_body_under_limit_is_processed(
    client_factory,
    settings: OrdersSettings,
) -> None:
    async with client_factory(app_settings=_limited(settings)) as client:
        response = await create(client, order_body(str(uuid.uuid4())))

    assert response.status_code == 201, response.text
