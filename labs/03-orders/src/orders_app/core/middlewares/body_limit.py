from http import HTTPStatus

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from orders_app.core.errors import problem

_TOO_LARGE = HTTPStatus.CONTENT_TOO_LARGE
# Тот же slug даёт и обработчик HTTPException для chunked-тела; его же показывает пример в OpenAPI
CONTENT_TOO_LARGE_SLUG = "content-too-large"
_DETAIL_MESSAGE_TEMPLATE = "Request body must not exceed {max_body_bytes} bytes"


class BodySizeLimitMiddleware:
    """
    Отклоняет тело запроса больше max_body_bytes ответом 413, не читая его в память целиком.
    - Есть Content-Length: отвечаем сразу, тело не читаем вовсе.
    - Нет Content-Length (chunked): считаем байты по мере чтения и прерываем, как только превышен лимит.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        content_length = dict(scope["headers"]).get(b"content-length")
        if content_length is not None and content_length.isdigit() and int(content_length) > self.max_body_bytes:
            response = problem(
                Request(scope),
                _TOO_LARGE,
                CONTENT_TOO_LARGE_SLUG,
                _TOO_LARGE.phrase,
                _DETAIL_MESSAGE_TEMPLATE.format(max_body_bytes=self.max_body_bytes),
            )
            await response(scope, receive, send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    # Именно HTTPException, потому что прочие исключения при чтении тела FastAPI превращает в 400.
                    # Обработчик HTTPException отдаст ту же problem 413, что и ветка с Content-Length.
                    raise HTTPException(
                        _TOO_LARGE,
                        detail=_DETAIL_MESSAGE_TEMPLATE.format(max_body_bytes=self.max_body_bytes),
                    )

            return message

        await self.app(scope, limited_receive, send)
