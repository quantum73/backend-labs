import asyncio

import httpx
import pytest

from aggregator_app.upstream import FakeMarketplaceTransport, SourceProfile, compute_signature


@pytest.mark.asyncio
async def test_fake_upstream_returns_signed_offer():
    transport = FakeMarketplaceTransport({"alpha": SourceProfile(latency_s=0)}, seed=1)
    async with httpx.AsyncClient(transport=transport, base_url="https://alpha.example") as client:
        response = await client.get("/offers", params={"sku": "SKU-1"})

    offer = response.json()
    assert response.status_code == 200
    assert offer["signature"] == compute_signature("alpha", "SKU-1", offer["price_kopecks"])


@pytest.mark.asyncio
async def test_fake_upstream_counts_cancelled_requests():
    transport = FakeMarketplaceTransport({"alpha": SourceProfile(hang_rate=1.0)}, seed=1)
    async with httpx.AsyncClient(transport=transport, base_url="https://alpha.example") as client:
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.05):
                await client.get("/offers", params={"sku": "SKU-1"})

    assert transport.stats.cancelled["alpha"] == 1
    assert transport.stats.total_in_flight == 0
