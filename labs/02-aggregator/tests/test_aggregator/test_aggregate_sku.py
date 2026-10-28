import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from aggregator_app.aggregator import aggregate_sku
from aggregator_app.models import Failed, FailReason, Fetched, Offer, SourceResult

_SKU = "sku-01"
_SOURCES = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]


@dataclass
class _StubFetcher:
    """Стаб по контракту OfferFetcher: заданный ответ и задержка на площадку, журнал вызовов."""

    results: Mapping[str, SourceResult] = field(default_factory=dict)
    delays: Mapping[str, float] = field(default_factory=dict)
    errors: Mapping[str, Exception] = field(default_factory=dict)  # площадка «с багом»: бросает вместо ответа
    calls: list[tuple[str, float]] = field(default_factory=list)  # (source, deadline)
    completed: list[str] = field(default_factory=list)  # площадки в порядке завершения
    cancelled: set[str] = field(default_factory=set)  # площадки, чей fetch отменили

    async def fetch(self, source: str, sku: str, *, deadline: float) -> SourceResult:
        self.calls.append((source, deadline))  # запись появляется, только если корутина запущена
        try:
            await asyncio.sleep(self.delays.get(source, 0))
        except asyncio.CancelledError:
            self.cancelled.add(source)
            raise

        if source in self.errors:
            raise self.errors[source]

        self.completed.append(source)
        return self.results[source]


def _fetched(source: str, price: int) -> Fetched:
    offer = Offer(source=source, sku=_SKU, price_kopecks=price, currency="RUB", signature="sig")
    return Fetched(offer=offer, attempts=1, elapsed_s=0.01)


def _failed(reason: FailReason, status_code: int | None = None) -> Failed:
    return Failed(reason=reason, attempts=1, elapsed_s=0.01, status_code=status_code)


@pytest.mark.asyncio
async def test_aggregate_sku_all_sources_are_down():
    fake_fetcher = _StubFetcher(
        results={
            "alpha": _failed(reason=FailReason.TIMEOUT, status_code=None),
            "beta": _failed(reason=FailReason.HTTP_ERROR, status_code=502),
            "gamma": _failed(reason=FailReason.TRANSPORT_ERROR, status_code=None),
            "delta": _failed(reason=FailReason.INVALID_RESPONSE, status_code=None),
            "epsilon": _failed(reason=FailReason.BAD_SIGNATURE, status_code=None),
            "zeta": _failed(reason=FailReason.HTTP_ERROR, status_code=503),
        },
    )
    result = await aggregate_sku(
        fetcher=fake_fetcher,
        sku=_SKU,
        sources=_SOURCES,
        deadline_s=1.0,
    )

    assert result.best is None
    assert result.offers_count == 0
    assert dict(result.sources) == {
        "alpha": "timeout",
        "beta": "http_502",
        "gamma": "transport_error",
        "delta": "invalid_response",
        "epsilon": "bad_signature",
        "zeta": "http_503",
    }
    assert list(result.sources) == _SOURCES


@pytest.mark.asyncio
async def test_sources_order_follows_input_not_completion():
    fetcher = _StubFetcher(
        results={source: _failed(FailReason.TIMEOUT) for source in _SOURCES},
        delays={source: 0.01 * n for n, source in enumerate(reversed(_SOURCES), start=1)},
    )

    result = await aggregate_sku(fetcher, _SKU, sources=_SOURCES, deadline_s=1.0)

    assert fetcher.completed == _SOURCES[::-1]
    assert list(result.sources) == _SOURCES


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sources", "results", "expected_best_source", "expected_offers_count"),
    [
        pytest.param(
            ["zeta", "beta", "alpha"],
            {
                "zeta": _fetched("zeta", 100),
                "beta": _fetched("beta", 200),
                "alpha": _fetched("alpha", 100),
            },
            "alpha",
            3,
            id="tie-break-all-fetched",
        ),
        pytest.param(
            ["delta", "zeta", "beta", "alpha", "gamma"],
            {
                "delta": _fetched("delta", 150),
                "zeta": _fetched("zeta", 100),
                "beta": _failed(reason=FailReason.HTTP_ERROR, status_code=503),
                "alpha": _fetched("alpha", 100),
                "gamma": _failed(reason=FailReason.TIMEOUT),
            },
            "alpha",
            3,
            id="tie-break-fetched-and-failed",
        ),
        pytest.param(
            ["beta", "gamma", "alpha"],
            {
                "beta": _fetched("beta", 300),
                "gamma": _failed(reason=FailReason.BAD_SIGNATURE),
                "alpha": _fetched("alpha", 250),
            },
            "alpha",
            2,
            id="unique-min-price",
        ),
    ],
)
async def test_best_offer_is_min_price_among_fetched(
    sources: list[str],
    results: Mapping[str, SourceResult],
    expected_best_source: str,
    expected_offers_count: int,
):
    fetcher = _StubFetcher(results=results)

    result = await aggregate_sku(fetcher, _SKU, sources=sources, deadline_s=1.0)

    expected_best = results[expected_best_source]
    assert isinstance(expected_best, Fetched)
    assert result.best == expected_best.offer
    assert result.offers_count == expected_offers_count
    assert list(result.sources) == sources


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deadline_s", "sources", "message"),
    [
        pytest.param(0.0, _SOURCES, "greater than 0", id="zero-deadline"),
        pytest.param(-1.0, _SOURCES, "greater than 0", id="negative-deadline"),
        pytest.param(float("nan"), _SOURCES, "finite", id="nan-deadline"),
        pytest.param(float("inf"), _SOURCES, "finite", id="inf-deadline"),
        pytest.param(1.0, [], "cannot be empty", id="empty-sources"),
        pytest.param(1.0, [*_SOURCES, "gamma"], "duplicates", id="duplicate-sources"),
        pytest.param(1.0, "alpha", "not a string", id="string-sources"),
        pytest.param(1.0, "", "not a string", id="empty-string-sources"),
    ],
)
async def test_invalid_parameters_raise_before_any_fetch(deadline_s, sources, message):
    fetcher = _StubFetcher(results={})

    with pytest.raises(ValueError, match=message):
        await aggregate_sku(fetcher, _SKU, sources=sources, deadline_s=deadline_s)

    assert fetcher.calls == []


@pytest.mark.asyncio
async def test_elapsed_ms_is_in_milliseconds_and_sources_run_concurrently():
    fetcher = _StubFetcher(
        results={source: _failed(FailReason.TIMEOUT) for source in _SOURCES},
        delays=dict.fromkeys(_SOURCES, 0.1),
    )

    result = await aggregate_sku(fetcher, _SKU, sources=_SOURCES, deadline_s=1.0)
    assert 100 <= result.elapsed_ms < 200


@pytest.mark.asyncio
async def test_aggregate_sku_deadline():
    fetcher = _StubFetcher(
        results={source: _failed(FailReason.TIMEOUT) for source in _SOURCES},
    )

    deadline_s = 1.0
    expected_deadline = asyncio.get_running_loop().time() + deadline_s
    await aggregate_sku(fetcher, _SKU, sources=_SOURCES, deadline_s=deadline_s)

    assert [source for source, _ in fetcher.calls] == _SOURCES  # дедлайн получили все площадки
    unique_deadlines = {deadline for _, deadline in fetcher.calls}
    assert len(unique_deadlines) == 1
    assert unique_deadlines.pop() == pytest.approx(expected_deadline, abs=0.01)


@pytest.mark.asyncio
async def test_fetcher_bug_propagates_and_cancels_other_fetches():
    broken = "gamma"
    fetcher = _StubFetcher(
        results={source: _failed(FailReason.TIMEOUT) for source in _SOURCES if source != broken},
        errors={broken: KeyError("bug in fetcher")},
        # Сломанная площадка бросает через 10 мс: к этому моменту остальные уже стоят на своём await
        delays={source: 0.01 if source == broken else 10 for source in _SOURCES},
    )

    async with asyncio.timeout(1):  # страховка от сломанного теста
        with pytest.RaisesGroup(KeyError):
            await aggregate_sku(fetcher, _SKU, sources=_SOURCES, deadline_s=1.0)

    assert len(fetcher.calls) == len(_SOURCES)  # все стартовали, иначе отмена ничего бы не доказала
    assert fetcher.cancelled == set(_SOURCES) - {broken}
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def _wait_until_started(fetcher: _StubFetcher, count: int) -> None:
    async with asyncio.timeout(1):
        while len(fetcher.calls) < count:  # noqa: ASYNC110 — ждём журнал стаба, событий у него нет
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_cancel_propagates_to_all_fetches():
    fetcher = _StubFetcher(
        results={source: _failed(FailReason.TIMEOUT) for source in _SOURCES},
        delays=dict.fromkeys(_SOURCES, 10),
    )
    task = asyncio.create_task(aggregate_sku(fetcher, _SKU, sources=_SOURCES, deadline_s=1.0))
    await _wait_until_started(fetcher, len(_SOURCES))

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert task.cancelled()
    assert fetcher.cancelled == set(_SOURCES)
    assert asyncio.all_tasks() == {asyncio.current_task()}
