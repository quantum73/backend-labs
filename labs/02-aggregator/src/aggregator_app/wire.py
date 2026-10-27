"""
Почему pydantic TypeAdapter над TypedDict, а не msgspec или ручной разбор:
- pydantic уже есть в зависимостях, новая зависимость не нужна;
- validate_json разбирает байты сразу в типизированный словарь;
- strict=True не приводит "100", 100.0 и true в price_kopecks, так как это ошибка
и площадка, приславшая цену строкой, нарушает контракт;
- все виды порчи (не JSON, не объект, нет поля, не тот тип, цена ``<= 0``) дают одно исключение ValidationError.
"""

from typing import Annotated, TypedDict

from pydantic import ConfigDict, Field, TypeAdapter, with_config

from aggregator_app.models import Offer


@with_config(ConfigDict(strict=True))
class _OfferPayload(TypedDict):
    source: str
    sku: str
    price_kopecks: Annotated[int, Field(gt=0)]
    currency: str
    signature: str


_OFFER_PAYLOAD = TypeAdapter(_OfferPayload)


def parse_offer(raw: bytes) -> Offer:
    return Offer(**_OFFER_PAYLOAD.validate_json(raw))
