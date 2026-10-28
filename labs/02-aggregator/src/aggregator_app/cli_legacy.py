"""Запуск старой версии: uv run aggregator-legacy --skus 50 --seed 1"""

import argparse
import asyncio
import json
import time

from aggregator_app.legacy import aggregate_all
from aggregator_app.upstream import FakeMarketplaceTransport


def main() -> int:
    parser = argparse.ArgumentParser(description="Legacy offers aggregator")
    parser.add_argument("--skus", type=int, default=50)
    parser.add_argument("--seed", type=int, default=None)
    args = parser.parse_args()

    transport = FakeMarketplaceTransport(seed=args.seed)
    skus = [f"SKU-{i:05d}" for i in range(args.skus)]

    started = time.perf_counter()
    results = asyncio.run(aggregate_all(transport, skus))
    elapsed = time.perf_counter() - started

    for result in results:
        print(json.dumps(result, ensure_ascii=False))  # noqa: T201
    stats = transport.stats
    print(  # noqa: T201
        f"done skus={len(results)} elapsed={elapsed:.2f}s "
        f"requests={sum(stats.requests_total.values())} max_in_flight={stats.max_total_in_flight}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
