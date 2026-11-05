from enum import StrEnum, auto


class OrderStatus(StrEnum):
    CREATED = auto()


class IdempotencyKeyScope(StrEnum):
    CREATE_ORDER = auto()
