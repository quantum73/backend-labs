import random
from http import HTTPStatus

RETRYABLE_STATUSES = frozenset({HTTPStatus.BAD_GATEWAY, HTTPStatus.SERVICE_UNAVAILABLE, HTTPStatus.GATEWAY_TIMEOUT})


def is_retryable_status(status_code: int) -> bool:
    return status_code in RETRYABLE_STATUSES


def full_jitter(*, attempt: int, base_s: float, cap_s: float, rng: random.Random) -> float:
    if attempt < 1:
        raise ValueError("attempt starts from 1")
    return rng.uniform(0, min(cap_s, base_s * 2 ** (attempt - 1)))


def parse_retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    value = value.strip()
    if not (value.isascii() and value.isdecimal()):
        return None
    return float(value)
