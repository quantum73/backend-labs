from enum import StrEnum, auto
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints

SkuStr = Annotated[str, StringConstraints(pattern=r"^[A-Z0-9][A-Z0-9-]{0,63}$")]


class OrderStatus(StrEnum):
    CREATED = auto()


class IdempotencyKeyScope(StrEnum):
    CREATE_ORDER = auto()


class CreateOrderRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: UUID
    currency: Literal["RUB"]
    items: list[OrderItemIn] = Field(min_length=1, max_length=100)


class OrderItemIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sku: SkuStr
    quantity: int = Field(ge=1, le=1000, strict=True)
    unit_price_kopecks: int = Field(ge=1, le=10**12, strict=True)


class OrderItemOut(BaseModel):
    sku: str
    quantity: int
    unit_price_kopecks: int


class OrderResponse(BaseModel):
    id: UUID
    customer_id: UUID
    status: OrderStatus
    currency: Literal["RUB"]
    items: list[OrderItemOut]
    total_kopecks: int
    created_at: AwareDatetime


class PaginatedOrderResponse(BaseModel):
    items: list[OrderResponse]
    next_cursor: str | None
