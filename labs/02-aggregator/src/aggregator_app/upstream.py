"""Имитация внешних площадок (менять не нужно).

Каждая площадка — это хост ``https://<source>.example``, отвечающий на ``GET /offers?sku=...``.
Транспорт подключается к ``httpx.AsyncClient(transport=...)`` вместо сети, поэтому всё работает
без интернета и детерминированно (через ``seed``).

Транспорт ведёт счётчики, на которые опираются тесты:
- ``requests_total`` / ``in_flight`` / ``max_in_flight`` — по каждой площадке и суммарно;
- ``cancelled`` — сколько запросов было отменено клиентом посреди обработки.
"""

import asyncio
import hashlib
import random
from collections import Counter
from dataclasses import dataclass, field
from http import HTTPStatus

import httpx

SIGNATURE_SALT = b"bazaar-offers"
SIGNATURE_ITERATIONS = 60_000


def compute_signature(source: str, sku: str, price_kopecks: int) -> str:
    """CPU-bound: PBKDF2 занимает порядка десятков миллисекунд на один вызов."""
    payload = f"{source}:{sku}:{price_kopecks}".encode()
    return hashlib.pbkdf2_hmac("sha256", payload, SIGNATURE_SALT, SIGNATURE_ITERATIONS).hex()


@dataclass(frozen=True, slots=True)
class SourceProfile:
    latency_s: float = 0.05
    jitter_s: float = 0.0
    error_rate: float = 0.0  # доля ответов 503
    hang_rate: float = 0.0  # доля запросов, которые «зависают» на hang_s
    hang_s: float = 30.0
    fail_first: int = 0  # первые N запросов к площадке получают 503 (для тестов ретраев)
    status: int = HTTPStatus.OK  # принудительный статус ответа (например, 404)


DEFAULT_PROFILES: dict[str, SourceProfile] = {
    "alpha": SourceProfile(latency_s=0.03),
    "beta": SourceProfile(latency_s=0.05, jitter_s=0.05),
    "gamma": SourceProfile(latency_s=0.08, error_rate=0.15),
    "delta": SourceProfile(latency_s=0.05, hang_rate=0.05),
    "epsilon": SourceProfile(latency_s=0.3, jitter_s=0.2),
    "zeta": SourceProfile(latency_s=0.04, error_rate=0.05, hang_rate=0.02),
}


@dataclass
class _Stats:
    requests_total: Counter[str] = field(default_factory=Counter)
    in_flight: Counter[str] = field(default_factory=Counter)
    max_in_flight: Counter[str] = field(default_factory=Counter)
    cancelled: Counter[str] = field(default_factory=Counter)
    total_in_flight: int = 0
    max_total_in_flight: int = 0


class FakeMarketplaceTransport(httpx.AsyncBaseTransport):
    def __init__(self, profiles: dict[str, SourceProfile] | None = None, *, seed: int | None = None) -> None:
        self.profiles = dict(DEFAULT_PROFILES if profiles is None else profiles)
        self.stats = _Stats()
        self._rng = random.Random(seed)  # noqa: S311 — имитация задержек, не криптография

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        source = request.url.host.removesuffix(".example")
        profile = self.profiles.get(source)
        if profile is None or request.url.path != "/offers":
            return httpx.Response(404, json={"error": "not_found"}, request=request)

        sku = request.url.params.get("sku", "")
        stats = self.stats
        stats.requests_total[source] += 1
        attempt_no = stats.requests_total[source]
        stats.in_flight[source] += 1
        stats.max_in_flight[source] = max(stats.max_in_flight[source], stats.in_flight[source])
        stats.total_in_flight += 1
        stats.max_total_in_flight = max(stats.max_total_in_flight, stats.total_in_flight)
        try:
            delay = profile.latency_s + self._rng.uniform(0, profile.jitter_s)
            if self._rng.random() < profile.hang_rate:
                delay = profile.hang_s
            await asyncio.sleep(delay)

            if attempt_no <= profile.fail_first or self._rng.random() < profile.error_rate:
                return httpx.Response(503, json={"error": "unavailable"}, request=request)
            if profile.status != HTTPStatus.OK:
                return httpx.Response(profile.status, json={"error": "forced"}, request=request)

            price = self._rng.randint(10_000, 500_000)
            # «Сервер» считает подпись в отдельном потоке, не блокируя общий event loop
            signature = await asyncio.to_thread(compute_signature, source, sku, price)
            body = {"source": source, "sku": sku, "price_kopecks": price, "currency": "RUB", "signature": signature}
            return httpx.Response(200, json=body, request=request)
        except asyncio.CancelledError:
            stats.cancelled[source] += 1
            raise
        finally:
            stats.in_flight[source] -= 1
            stats.total_in_flight -= 1
