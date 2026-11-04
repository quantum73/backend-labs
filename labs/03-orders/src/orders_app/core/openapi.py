"""
Описание ошибок в OpenAPI.
Примеры собираются из классов ошибок и констант обработчиков, поэтому документация не может отстать от кода.
Например, поменяли title у NotFoundError - изменился и пример в /docs.
"""

from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, Field

from orders_app.core.errors import (
    INTERNAL_ERROR_SLUG,
    INTERNAL_ERROR_TITLE,
    PROBLEM_JSON,
    TYPE_BASE,
    VALIDATION_ERROR_SLUG,
    VALIDATION_ERROR_TITLE,
    DatabaseUnavailableError,
    ProblemError,
)

_REF_TEMPLATE = "#/components/schemas/{model}"
EXAMPLE_ORDER_ID = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
RETRY_AFTER_HEADER = {
    "Retry-After": {
        "description": "Seconds to wait before retrying the request",
        "schema": {"type": "integer"},
    },
}


class ValidationErrorItem(BaseModel):
    loc: list[str | int] = Field(description="Path to the field, e.g. ['body', 'customer_id']")
    msg: str = Field(description="What is wrong with the value")
    type: str = Field(description="Machine-readable validation error code")


class ProblemDetails(BaseModel):
    """Body of every API error, RFC 9457, media type application/problem+json."""

    type: str = Field(description="Error type URI: a stable identifier, branch on it rather than on the text")
    title: str = Field(description="Short summary of the error type, the same for every occurrence")
    status: int = Field(description="The HTTP status code of the response")
    detail: str | None = Field(
        default=None, description="Explanation of this occurrence, when there is something to add"
    )
    instance: str = Field(description="Request path where the error occurred")
    errors: list[ValidationErrorItem] | None = Field(
        default=None,
        description="validation-error only: fields that failed validation. Submitted values are never echoed",
    )


def problem_example(
    slug: str,
    title: str,
    status: int,
    *,
    instance: str,
    detail: str | None = None,
    **extensions: Any,
) -> dict[str, Any]:
    # Та же форма, что собирает errors.problem(): без detail, если его нет
    example: dict[str, Any] = {"type": TYPE_BASE + slug, "title": title, "status": status, "instance": instance}
    if detail:
        example["detail"] = detail
    return example | extensions


def error_example(error: type[ProblemError], *, instance: str, detail: str) -> dict[str, Any]:
    return problem_example(error.slug, error.title, int(error.status), instance=instance, detail=detail)


def problem_response(
    description: str,
    examples: dict[str, dict[str, Any]],
    *,
    headers: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Ответ-ошибка для responses= у маршрута: problem+json со ссылкой на ProblemDetails и именованными примерами.
    """
    response: dict[str, Any] = {
        "description": description,
        "content": {
            PROBLEM_JSON: {
                "schema": {"$ref": _REF_TEMPLATE.format(model=ProblemDetails.__name__)},
                "examples": {name: {"value": value} for name, value in examples.items()},
            },
        },
    }
    if headers:
        response["headers"] = headers

    return response


VALIDATION_EXAMPLE = problem_example(
    VALIDATION_ERROR_SLUG,
    VALIDATION_ERROR_TITLE,
    422,
    instance="/v1/orders",
    errors=[{"loc": ["body", "customer_id"], "msg": "Input should be a valid UUID", "type": "uuid_parsing"}],
)

# Ошибки, которые может вернуть любой маршрут с параметрами и БД. Подключаются на уровне роутера:
# иначе FastAPI сам описывает 422 как application/json со своей схемой, а сервер такого не отдаёт
COMMON_PROBLEM_RESPONSES: dict[int | str, dict[str, Any]] = {
    422: problem_response(
        "Request validation failed",
        {VALIDATION_ERROR_SLUG: VALIDATION_EXAMPLE},
    ),
    500: problem_response(
        "Unexpected server error. Details are only in the logs",
        {
            INTERNAL_ERROR_SLUG: problem_example(
                INTERNAL_ERROR_SLUG,
                INTERNAL_ERROR_TITLE,
                500,
                instance="/v1/orders",
            )
        },
    ),
    503: problem_response(
        "Database is temporarily unavailable, retry after Retry-After seconds",
        {
            DatabaseUnavailableError.slug: error_example(
                DatabaseUnavailableError,
                instance="/v1/orders",
                detail="Could not get a database connection",
            ),
        },
        headers=RETRY_AFTER_HEADER,
    ),
}


def install_problem_schema(app: FastAPI) -> None:
    """
    Регистрирует ProblemDetails в components.schemas, на него ссылаются все ответы-ошибки через $ref.
    FastAPI кладёт в components только модели из response_model и тел запросов, а модель в responses=
    он описал бы как application/json. Поэтому дописываем схему в готовый документ сами.
    """
    build_openapi = app.openapi

    def openapi() -> dict[str, Any]:
        if app.openapi_schema is not None:
            return app.openapi_schema

        schema = build_openapi()
        definition = ProblemDetails.model_json_schema(ref_template=_REF_TEMPLATE)
        components = schema.setdefault("components", {}).setdefault("schemas", {})
        components.update(definition.pop("$defs", {}))  # ValidationErrorItem
        components[ProblemDetails.__name__] = definition
        return schema

    app.openapi = openapi  # type: ignore[method-assign]  # штатный способ FastAPI дополнить схему
