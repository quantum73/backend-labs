"""Граница вывода: SkuResult → одна строка JSON.

Два шага: явное преобразование домена в форму вывода (_SkuResultPayload), затем сериализация.
Сериализация через json.dumps, а не pydantic TypeAdapter:
- после преобразования остаётся обычный словарь из str/int/float/None, валидировать нечего;
- форму словаря проверяет mypy на этапе преобразования (TypedDict), в рантайме проверка не нужна;
- все решения о формате (имена ключей, состав best, округление) видны в одной функции, без сериализаторов в аннотациях.
"""

import json
from typing import TypedDict

from aggregator_app.aggregator import SkuResult

MS_PRECISION = 1  # 0.1 мс: меньше — шум таймеров, а не точность


class _OfferPayload(TypedDict):
    source: str
    price_kopecks: int
    currency: str


class _SkuResultPayload(TypedDict):
    sku: str
    best: _OfferPayload | None
    offers: int
    sources: dict[str, str]
    elapsed_ms: float


def _to_payload(sku_result: SkuResult) -> _SkuResultPayload:
    best_offer = sku_result.best
    return {
        "sku": sku_result.sku,
        "best": None
        if best_offer is None
        else {
            "source": best_offer.source,
            "price_kopecks": best_offer.price_kopecks,
            "currency": best_offer.currency,
        },
        "offers": sku_result.offers_count,
        "sources": dict(sku_result.sources),
        "elapsed_ms": round(sku_result.elapsed_ms, MS_PRECISION),
    }


def sku_result_to_string(sku_result: SkuResult) -> str:
    return json.dumps(_to_payload(sku_result), separators=(",", ":"), ensure_ascii=False)
