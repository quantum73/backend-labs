import asyncio
import random
from dataclasses import dataclass

import pytest

from aggregator_app.aggregator import OfferFetcher, aggregate_sku
from aggregator_app.client import UpstreamClient, UpstreamConfig
from aggregator_app.models import SourceResult
from aggregator_app.upstream import FakeMarketplaceTransport, SourceProfile

_SKU = "SKU-00001"
_SOURCES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
_HANGING = "delta"
_DEADLINE_S = 0.3
_TOLERANCE_S = 0.05


def _transport() -> FakeMarketplaceTransport:
    profiles = dict.fromkeys(_SOURCES, SourceProfile(latency_s=0.01)) | {_HANGING: SourceProfile(hang_rate=1.0)}
    return FakeMarketplaceTransport(profiles, seed=1)


def _client(transport: FakeMarketplaceTransport) -> UpstreamClient:
    return UpstreamClient(UpstreamConfig(), transport=transport, rng=random.Random(1))


@pytest.mark.asyncio
async def test_partial_result_with_hanging_source():
    transport = _transport()
    async with _client(transport) as client:
        loop = asyncio.get_running_loop()
        started = loop.time()
        # Страховка самого теста
        async with asyncio.timeout(_DEADLINE_S + 1):
            result = await aggregate_sku(client, _SKU, sources=_SOURCES, deadline_s=_DEADLINE_S)

        elapsed = loop.time() - started

    assert elapsed <= _DEADLINE_S + _TOLERANCE_S
    assert result.sources[_HANGING] == "timeout"
    assert {source: status for source, status in result.sources.items() if source != _HANGING} == dict.fromkeys(
        set(_SOURCES) - {_HANGING}, "ok"
    )
    assert result.best is not None
    assert result.best.source != _HANGING
    assert result.offers_count == len(_SOURCES) - 1
    assert transport.stats.cancelled[_HANGING] == 1
    assert transport.stats.total_in_flight == 0


@dataclass
class _BuggyFetcher:
    """Обёртка над настоящим клиентом: для одной площадки «баг» вместо запроса."""

    inner: OfferFetcher
    broken_source: str

    async def fetch(self, source: str, sku: str, *, deadline: float) -> SourceResult:
        if source == self.broken_source:
            await asyncio.sleep(0.05)
            raise KeyError("bug in fetcher")
        return await self.inner.fetch(source, sku, deadline=deadline)


@pytest.mark.asyncio
async def test_bug_wins_over_partial_result():
    transport = _transport()
    async with _client(transport) as client:
        loop = asyncio.get_running_loop()
        started = loop.time()

        with pytest.RaisesGroup(KeyError):
            await aggregate_sku(
                _BuggyFetcher(client, broken_source="beta"),
                _SKU,
                sources=_SOURCES,
                deadline_s=_DEADLINE_S,
            )

        elapsed = loop.time() - started

    assert elapsed < _DEADLINE_S
    assert transport.stats.cancelled[_HANGING] == 1
    assert transport.stats.total_in_flight == 0
    assert asyncio.all_tasks() == {asyncio.current_task()}
