import json
import uuid
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from orders_app.core.errors import PROBLEM_JSON, TYPE_BASE
from orders_app.core.openapi import ProblemDetails
from orders_app.main import create_app
from orders_app.settings import OrdersSettings
from tests.acceptance.helpers import order_body

PROBLEM_REF = "#/components/schemas/ProblemDetails"


@pytest.fixture
def spec(settings: OrdersSettings) -> dict[str, Any]:
    return create_app(settings).openapi()


def _error_responses(spec: dict[str, Any]) -> Iterator[tuple[str, str, dict[str, Any]]]:
    for path, operations in spec["paths"].items():
        for method, operation in operations.items():
            for code, response in operation["responses"].items():
                if int(code) >= 400:
                    yield f"{method.upper()} {path} {code}", code, response


def test_every_error_is_documented_as_problem_json(spec: dict[str, Any]) -> None:
    # Ни одна ошибка не описана как application/json: сервер их так не отдаёт
    wrong = {
        name: list(response.get("content", {}))
        for name, _, response in _error_responses(spec)
        if list(response.get("content", {})) != [PROBLEM_JSON]
        or response["content"][PROBLEM_JSON]["schema"] != {"$ref": PROBLEM_REF}
    }
    assert not wrong, wrong


def test_problem_schema_is_registered_without_dangling_refs(spec: dict[str, Any]) -> None:
    assert {"ProblemDetails", "ValidationErrorItem"} <= spec["components"]["schemas"].keys()
    assert "#/$defs" not in json.dumps(spec)  # $ref из pydantic должны указывать в components


def test_examples_match_schema_and_status(spec: dict[str, Any]) -> None:
    examples = [
        (name, code, example["value"])
        for name, code, response in _error_responses(spec)
        for example in response["content"][PROBLEM_JSON]["examples"].values()
    ]
    assert examples
    for name, code, value in examples:
        ProblemDetails.model_validate(value)
        assert value["status"] == int(code), name
        assert value["type"].startswith(TYPE_BASE), name


def test_post_documents_idempotency_contract(spec: dict[str, Any]) -> None:
    create = spec["paths"]["/v1/orders"]["post"]
    [header] = [p for p in create["parameters"] if p["in"] == "header" and p["name"] == "idempotency-key"]
    assert "400" in header["description"]
    assert {"Location", "Idempotent-Replayed"} <= create["responses"]["201"]["headers"].keys()
    assert {"validation-error", "idempotency-key-reuse"} <= create["responses"]["422"]["content"][PROBLEM_JSON][
        "examples"
    ].keys()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("method", "path", "kwargs", "status"),
    [
        pytest.param("GET", f"/v1/orders/{uuid.uuid4()}", {}, 404, id="not-found"),
        pytest.param("GET", "/v1/orders/not-a-uuid", {}, 422, id="validation-error"),
        pytest.param("POST", "/v1/orders", {"json": order_body()}, 400, id="idempotency-key-required"),
        pytest.param(
            "GET",
            "/v1/orders",
            {"params": {"customer_id": str(uuid.uuid4()), "cursor": "!!!"}},
            400,
            id="invalid-cursor",
        ),
    ],
)
async def test_real_error_bodies_match_documented_schema(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    kwargs: dict[str, Any],
    status: int,
) -> None:
    # Реальный ответ проверяется той же моделью, что описана в OpenAPI: схема не может отстать от обработчиков
    response = await client.request(method, path, **kwargs)

    assert response.status_code == status
    body = response.json()
    ProblemDetails.model_validate(body)
    assert set(body) <= ProblemDetails.model_fields.keys(), set(body) - ProblemDetails.model_fields.keys()


def test_health_probes_are_not_in_public_contract(spec: dict[str, Any]) -> None:
    assert not {"/livez", "/readyz"} & spec["paths"].keys()
