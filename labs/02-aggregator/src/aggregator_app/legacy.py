"""Агрегатор предложений, доставшийся «от коллеги».

Для каждого SKU опрашивает все площадки, проверяет подписи и выбирает лучшую цену.
На проде сервис периодически «замирает» на секунды, не укладывается в SLA и плохо
переживает рестарты. Задача лабы — найти и исправить причины. Этот файл не правим:
он остаётся эталоном «как было», новый код пишется рядом.
"""

import asyncio
import hashlib
import hmac
import time
from typing import Any

import httpx

from aggregator_app.upstream import SIGNATURE_ITERATIONS, SIGNATURE_SALT

SOURCES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]


async def fetch_offer(transport: httpx.AsyncBaseTransport, source: str, sku: str) -> dict[str, Any] | None:
    client = httpx.AsyncClient(transport=transport, base_url=f"https://{source}.example")
    for attempt in range(3):
        try:
            response = await client.get("/offers", params={"sku": sku})
            response.raise_for_status()
            offer: dict[str, Any] = response.json()
            return offer
        except:
            time.sleep(0.1 * 2**attempt)
    return None


def verify_signature(offer: dict[str, Any]) -> bool:
    payload = f"{offer['source']}:{offer['sku']}:{offer['price_kopecks']}".encode()
    expected = hashlib.pbkdf2_hmac("sha256", payload, SIGNATURE_SALT, SIGNATURE_ITERATIONS).hex()
    return hmac.compare_digest(expected, offer["signature"])


async def report_metrics(sku: str, offers_count: int) -> None:
    await asyncio.sleep(0.01)  # «отправка» метрик
    print(f"metrics sku={sku} offers={offers_count}")


async def aggregate_sku(transport: httpx.AsyncBaseTransport, sku: str) -> dict[str, Any]:
    tasks = [asyncio.create_task(fetch_offer(transport, source, sku)) for source in SOURCES]
    offers = await asyncio.gather(*tasks)
    valid = [offer for offer in offers if offer is not None and verify_signature(offer)]
    best = min(valid, key=lambda offer: offer["price_kopecks"]) if valid else None
    asyncio.create_task(report_metrics(sku, len(valid)))
    return {"sku": sku, "best": best, "offers": len(valid)}


async def aggregate_all(transport: httpx.AsyncBaseTransport, skus: list[str]) -> list[dict[str, Any]]:
    return await asyncio.gather(*(aggregate_sku(transport, sku) for sku in skus))
