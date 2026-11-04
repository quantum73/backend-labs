import re
import time
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = b"x-request-id"
REQUEST_ID_STATE_KEY = "request_id"
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.log = structlog.get_logger("http")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # lifespan, websocket — пропускаем как есть
            await self.app(scope, receive, send)
            return

        incoming = dict(scope["headers"]).get(REQUEST_ID_HEADER, b"").decode("latin-1")
        request_id = incoming if _VALID_REQUEST_ID.fullmatch(incoming) else str(uuid.uuid7())
        scope.setdefault("state", {})[REQUEST_ID_STATE_KEY] = request_id
        started = time.perf_counter()
        status_code = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = [*message.get("headers", []), (REQUEST_ID_HEADER, request_id.encode("latin-1"))]
                message = {**message, "headers": headers}
            await send(message)

        with structlog.contextvars.bound_contextvars(request_id=request_id):
            try:
                await self.app(scope, receive, send_wrapper)
            finally:
                route = scope.get("route")
                self.log.info(
                    "http_request",
                    method=scope["method"],
                    route=getattr(route, "path", "<unmatched>"),
                    status=status_code,
                    duration_ms=round((time.perf_counter() - started) * 1000, 1),
                )
