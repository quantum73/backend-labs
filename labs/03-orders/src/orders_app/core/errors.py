"""
Для ошибок используется стандарт формата ошибок HTTP API - RFC 9457.
Пример:
```
HTTP/1.1 422 Unprocessable Content
Content-Type: application/problem+json

{
  "type": "https://errors.orders.example/idempotency-key-reuse",
  "title": "Idempotency-Key was already used with a different request",
  "status": 422,
  "detail": "This Idempotency-Key was used with a different request body",
  "instance": "/v1/orders"
}
```
- type: URI типа ошибки - стабильный машиночитаемый идентификатор. Клиенты ветвятся по нему, а не по тексту.
- title: Короткое описание типа, одинаковое для всех экземпляров.
- status: Дублирует HTTP-статус. Полезно, когда тело передают дальше без заголовков.
- detail: Описание этого конкретного случая для человека.
- instance: Где произошло: путь или id запроса.
- Расширения: Любые дополнительные поля: errors для валидации, retry_after, balance.
"""

from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, ClassVar

import structlog
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.requests import Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from orders_app.core.middlewares import REQUEST_ID_HEADER, REQUEST_ID_STATE_KEY

PROBLEM_JSON = "application/problem+json"
TYPE_BASE = "https://errors.orders.example/"


class ProblemError(Exception):
    """Прикладная ошибка, которая знает, как превратиться в Problem Details."""

    status: HTTPStatus = HTTPStatus.BAD_REQUEST
    slug: str = "bad-request"
    title: str = "Bad request"
    headers: ClassVar[Mapping[str, str]] = {}

    def __init__(self, detail: str, **extensions: Any) -> None:
        super().__init__(detail)
        self.detail = detail
        self.extensions = extensions


class NotFoundError(ProblemError):
    status = HTTPStatus.NOT_FOUND
    slug = "not-found"
    title = "Resource not found"


class IdempotencyKeyRequiredError(ProblemError):
    status = HTTPStatus.BAD_REQUEST
    slug = "idempotency-key-required"
    title = "Idempotency-Key is incorrect or not given"


class IdempotencyKeyReuseError(ProblemError):
    status = HTTPStatus.UNPROCESSABLE_CONTENT
    slug = "idempotency-key-reuse"
    title = "Idempotency-Key was already used with a different request"


class IdempotencyKeyInProgressError(ProblemError):
    """Запрос с этим ключом ещё выполняется: повтор не дождался оригинала за lock_timeout."""

    status = HTTPStatus.CONFLICT
    slug = "idempotency-key-in-progress"
    title = "A request with this Idempotency-Key is still in progress"
    headers: ClassVar[Mapping[str, str]] = {"Retry-After": "1"}


class DBNotReadyError(ProblemError):
    status = HTTPStatus.SERVICE_UNAVAILABLE
    slug = "not-ready"
    title = "Database is unavailable or is not ready"


class DatabaseUnavailableError(ProblemError):
    """Не удалось получить соединение с БД."""

    status = HTTPStatus.SERVICE_UNAVAILABLE
    slug = "db-unavailable"
    title = "Database is temporarily unavailable"
    headers: ClassVar[Mapping[str, str]] = {"Retry-After": "2"}


def problem(
    request: Request,
    status: int,
    slug: str,
    title: str,
    detail: str | None = None,
    **ext: Any,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": TYPE_BASE + slug,
        "title": title,
        "status": status,
        "instance": request.url.path,
    }
    if detail:
        body["detail"] = detail

    body.update(ext)
    return JSONResponse(body, status_code=status, media_type=PROBLEM_JSON)


async def _problem_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ProblemError)  # noqa: S101
    response = problem(request, exc.status, exc.slug, exc.title, exc.detail, **exc.extensions)
    response.headers.update(exc.headers)
    return response


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    """
    RequestValidationError:
    - 422 статус код;
    - slug = validation-error;
    - errors = [{loc, msg, type}] (без input!);
    """

    assert isinstance(exc, RequestValidationError)  # noqa: S101
    # Без поля input: в нём может быть номер карты, пароль, персональные данные
    errors = [{"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()]
    return problem(request, 422, "validation-error", "Request validation failed", errors=errors)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    """
    starlette.exceptions.HTTPException:
    - problem с тем же статусом
    - заголовки сохраняются (например, Allow у 405);
    """

    assert isinstance(exc, StarletteHTTPException)  # noqa: S101
    phrase = HTTPStatus(exc.status_code).phrase
    response = problem(request, exc.status_code, phrase.lower().replace(" ", "-"), phrase)
    if exc.headers:
        response.headers.update(exc.headers)

    return response


async def _unhandled(request: Request, _exc: Exception) -> JSONResponse:
    """
    Exception:
    - 500 internal-error;
    - Трейсбек только в лог (logger.exception), клиенту ничего внутреннего.
    """
    request_id: str | None = getattr(request.state, REQUEST_ID_STATE_KEY, None)
    structlog.get_logger().exception("unhandled_error", path=request.url.path, request_id=request_id)
    response = problem(request, 500, "internal-error", "Internal server error")
    if request_id is not None:
        response.headers[REQUEST_ID_HEADER.decode("latin-1")] = request_id
    return response


def install_problem_handlers(app: FastAPI) -> None:
    app.add_exception_handler(ProblemError, _problem_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(Exception, _unhandled)
