"""
Почему pydantic TypeAdapter над TypedDict, а не msgspec или ручной разбор:
- pydantic уже есть в зависимостях, новая зависимость не нужна;
- validate_json разбирает байты сразу в типизированный словарь;
- strict=True не приводит "100", 100.0 и true в price_kopecks, так как это ошибка
и площадка, приславшая цену строкой, нарушает контракт;
- все виды порчи (не JSON, не объект, нет поля, не тот тип, цена <= 0) дают одно исключение InvalidOfferPayloadError.
"""

from typing import TypedDict

from pydantic import ConfigDict, TypeAdapter, with_config

from aggregator_app.models import Offer


class InvalidOfferPayloadError(ValueError):
    """Ответ площадки не является корректным предложением."""


@with_config(ConfigDict(strict=True))
class _OfferPayload(TypedDict):
    source: str
    sku: str
    price_kopecks: int  # За валидацию отвечает доменная модель Offer
    currency: str
    signature: str


_OFFER_PAYLOAD = TypeAdapter(_OfferPayload)


def parse_offer(raw: bytes) -> Offer:
    try:
        return Offer(**_OFFER_PAYLOAD.validate_json(raw))
    except ValueError as exc:  # ValidationError — подкласс ValueError, поэтому одна ветка
        raise InvalidOfferPayloadError("invalid offer payload") from exc
