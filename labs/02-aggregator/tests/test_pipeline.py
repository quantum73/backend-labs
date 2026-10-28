import asyncio
import dataclasses
from collections.abc import Callable, Coroutine, Iterator
from dataclasses import dataclass, field

import pytest
from structlog.testing import capture_logs

from aggregator_app.aggregator import SkuResult
from aggregator_app.models import Failed, FailReason, Fetched, Offer, SourceResult
from aggregator_app.pipeline import PipelineConfig, RunSummary, ShutdownMode, run_pipeline

# Одна площадка: один fetch == один aggregate_sku, стаб считает именно SKU
CONFIG = PipelineConfig(sources=("alpha",), workers=3, queue_size=5, deadline_s=1.0, grace_period_s=1.0)
LONG_S = 5.0  # «долгий» SKU: заметно дольше любого grace в тестах, но в пределах дедлайна
TOLERANCE_S = 0.05


@dataclass
class _StubFetcher:
    """Стаб по контракту OfferFetcher с журналом: кто, когда начат, завершён, отменён, сколько одновременно."""

    delay_s: float = 0.0
    delays: dict[str, float] = field(default_factory=dict)  # sku → своя задержка
    buggy: frozenset[str] = frozenset()  # SKU, на которых fetch бросает KeyError (баг реализации)
    failing_sources: frozenset[str] = frozenset()  # площадки, отвечающие Failed(TIMEOUT)
    stop: asyncio.Event | None = None  # чтобы зафиксировать SKU, начатые после stop
    started: list[str] = field(default_factory=list)
    started_after_stop: list[str] = field(default_factory=list)
    cancelled: list[str] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0

    def fetch(self, source: str, sku: str, *, deadline: float) -> Coroutine[None, None, SourceResult]:
        # Старт фиксируется в момент вызова из aggregate_sku, то есть когда конвейер начал SKU.
        # В async def запись появилась бы только на следующей итерации loop, когда задача fetch получит управление,
        # и SKU, начатый до stop, выглядел бы начатым после.
        self.started.append(sku)
        if self.stop is not None and self.stop.is_set():
            self.started_after_stop.append(sku)

        return self._fetch(source, sku)

    async def _fetch(self, source: str, sku: str) -> SourceResult:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(self.delays.get(sku, self.delay_s))
        except asyncio.CancelledError:
            self.cancelled.append(sku)
            raise
        finally:
            self.in_flight -= 1

        if sku in self.buggy:
            raise KeyError(sku)
        if source in self.failing_sources:
            return Failed(FailReason.TIMEOUT, attempts=1, elapsed_s=0.01)

        offer = Offer(source=source, sku=sku, price_kopecks=100, currency="RUB", signature="sig")
        return Fetched(offer, attempts=1, elapsed_s=0.01)


def _skus(count: int) -> Iterator[str]:
    for n in range(count):
        yield f"SKU-{n:05d}"


def _assert_no_leaked_tasks() -> None:
    assert asyncio.all_tasks() == {asyncio.current_task()}


def _assert_accounted(summary: RunSummary) -> None:
    # RunSummary проверяет это и сам, тест фиксирует контракт явно
    assert summary.enqueued == (
        summary.processed + summary.not_started + summary.cancelled + summary.errors + summary.contract_violations
    )


def _stop_after(count: int, stop: asyncio.Event, emitted: list[SkuResult]) -> Callable[[SkuResult], None]:
    def emit(result: SkuResult) -> None:
        emitted.append(result)
        if len(emitted) == count:
            stop.set()

    return emit


@pytest.mark.asyncio
async def test_completed_run_emits_every_sku_exactly_once():
    fetcher = _StubFetcher(delay_s=0.001)
    emitted: list[SkuResult] = []

    summary = await run_pipeline(fetcher, _skus(50), config=CONFIG, emit=emitted.append, stop=asyncio.Event())

    assert sorted(result.sku for result in emitted) == list(_skus(50))
    assert summary.shutdown is ShutdownMode.COMPLETED
    assert (summary.enqueued, summary.processed, summary.not_started, summary.cancelled) == (50, 50, 0, 0)
    assert dict(summary.statuses) == {"ok": 50}
    _assert_accounted(summary)
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_statuses_are_counted_over_all_sources():
    fetcher = _StubFetcher(failing_sources=frozenset({"beta"}))
    config = dataclasses.replace(CONFIG, sources=("alpha", "beta"))

    summary = await run_pipeline(fetcher, _skus(10), config=config, emit=lambda _: None, stop=asyncio.Event())

    assert dict(summary.statuses) == {"ok": 10, "timeout": 10}
    with pytest.raises(TypeError):
        summary.statuses["ok"] = 0  # type: ignore[index]


@pytest.mark.asyncio
async def test_producer_is_backpressured_by_bounded_queue():
    fetcher = _StubFetcher(delay_s=0.005)
    emitted: list[SkuResult] = []
    max_ahead = 0

    def counting_skus() -> Iterator[str]:
        nonlocal max_ahead
        for yielded, sku in enumerate(_skus(60), start=1):
            max_ahead = max(max_ahead, yielded - len(emitted))
            yield sku

    summary = await run_pipeline(fetcher, counting_skus(), config=CONFIG, emit=emitted.append, stop=asyncio.Event())

    assert summary.processed == 60
    # SKU у воркеров + в очереди + один у producer, ждущего на put. Равенство: очередь действительно заполнялась
    assert max_ahead == CONFIG.workers + CONFIG.queue_size + 1


@pytest.mark.asyncio
async def test_at_most_workers_skus_in_flight():
    fetcher = _StubFetcher(delay_s=0.005)

    await run_pipeline(fetcher, _skus(30), config=CONFIG, emit=lambda _: None, stop=asyncio.Event())

    assert fetcher.max_in_flight == CONFIG.workers


@pytest.mark.asyncio
async def test_graceful_stop_drops_queued_and_finishes_started():
    stop = asyncio.Event()
    fetcher = _StubFetcher(delay_s=0.02, stop=stop)
    emitted: list[SkuResult] = []

    summary = await run_pipeline(
        fetcher,
        _skus(100),
        config=CONFIG,
        emit=_stop_after(5, stop, emitted),
        stop=stop,
    )

    assert summary.shutdown is ShutdownMode.GRACEFUL
    assert fetcher.started_after_stop == []
    assert fetcher.cancelled == []
    assert sorted(fetcher.started) == sorted(result.sku for result in emitted)  # начатые доработали и выданы
    assert summary.processed == len(emitted) >= 5
    assert summary.not_started > 0
    assert summary.cancelled == 0
    _assert_accounted(summary)
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_forced_when_started_skus_outlive_grace():
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    stopped_at: list[float] = []
    fetcher = _StubFetcher(delay_s=LONG_S)
    config = dataclasses.replace(CONFIG, deadline_s=LONG_S * 2, grace_period_s=0.1)

    def request_stop() -> None:
        stopped_at.append(loop.time())
        stop.set()

    loop.call_later(0.05, request_stop)
    summary = await run_pipeline(fetcher, _skus(100), config=config, emit=lambda _: None, stop=stop)

    assert loop.time() - stopped_at[0] <= config.grace_period_s + TOLERANCE_S
    assert summary.shutdown is ShutdownMode.FORCED
    assert summary.cancelled == CONFIG.workers
    assert sorted(fetcher.cancelled) == sorted(fetcher.started)
    assert summary.processed == 0
    _assert_accounted(summary)
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_force_during_grace_cancels_immediately():
    loop = asyncio.get_running_loop()
    stop, force = asyncio.Event(), asyncio.Event()
    fetcher = _StubFetcher(delay_s=LONG_S)
    config = dataclasses.replace(CONFIG, deadline_s=LONG_S * 2, grace_period_s=LONG_S)
    started = loop.time()

    loop.call_later(0.05, stop.set)
    loop.call_later(0.1, force.set)  # второй Ctrl+C
    summary = await run_pipeline(fetcher, _skus(100), config=config, emit=lambda _: None, stop=stop, force=force)

    assert loop.time() - started <= 0.1 + TOLERANCE_S
    assert summary.shutdown is ShutdownMode.FORCED
    assert summary.cancelled == CONFIG.workers
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_empty_input_completes():
    fetcher = _StubFetcher()

    summary = await run_pipeline(fetcher, iter(()), config=CONFIG, emit=lambda _: None, stop=asyncio.Event())

    assert summary.shutdown is ShutdownMode.COMPLETED
    assert (summary.enqueued, summary.processed) == (0, 0)
    assert fetcher.started == []
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_stop_before_start_reads_nothing():
    stop = asyncio.Event()
    stop.set()
    pulled: list[str] = []

    def tracked_skus() -> Iterator[str]:
        for sku in _skus(10):
            pulled.append(sku)
            yield sku

    summary = await run_pipeline(_StubFetcher(), tracked_skus(), config=CONFIG, emit=lambda _: None, stop=stop)

    assert summary.shutdown is ShutdownMode.GRACEFUL
    assert summary.enqueued == 0
    assert pulled == []
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_stop_with_the_last_result_is_still_completed():
    stop = asyncio.Event()
    emitted: list[SkuResult] = []

    summary = await run_pipeline(
        _StubFetcher(delay_s=0.001),
        _skus(5),
        config=CONFIG,
        emit=_stop_after(5, stop, emitted),
        stop=stop,
    )

    # Вход исчерпан, ничего не выброшено и не отменено: работа не потеряна, значит, COMPLETED
    assert summary.shutdown is ShutdownMode.COMPLETED
    assert summary.processed == 5


@pytest.mark.asyncio
async def test_bug_on_one_sku_is_isolated_logged_and_counted():
    fetcher = _StubFetcher(buggy=frozenset({"SKU-00003"}))
    emitted: list[SkuResult] = []

    with capture_logs() as logs:
        summary = await run_pipeline(fetcher, _skus(10), config=CONFIG, emit=emitted.append, stop=asyncio.Event())

    assert summary.errors == 1
    assert summary.processed == 9
    assert "SKU-00003" not in {result.sku for result in emitted}
    assert summary.shutdown is ShutdownMode.COMPLETED  # баг не меняет режим, он влияет на код выхода через errors
    [failure] = [entry for entry in logs if entry["event"] == "sku_failed"]
    assert failure["sku"] == "SKU-00003"
    assert failure["log_level"] == "error"
    assert failure["exc_info"] is True  # трейсбек будет в логе
    _assert_accounted(summary)
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_watchdog_reports_contract_violation():
    hanging = "SKU-00002"
    fetcher = _StubFetcher(delays={hanging: LONG_S})
    config = dataclasses.replace(CONFIG, deadline_s=0.05, watchdog_margin_s=0.05)
    loop = asyncio.get_running_loop()
    started = loop.time()

    with capture_logs() as logs:
        summary = await run_pipeline(fetcher, _skus(6), config=config, emit=lambda _: None, stop=asyncio.Event())

    assert loop.time() - started <= config.deadline_s + config.watchdog_margin_s + TOLERANCE_S
    assert summary.contract_violations == 1
    assert summary.errors == 0
    assert summary.processed == 5
    assert fetcher.cancelled == [hanging]
    [violation] = [entry for entry in logs if entry["event"] == "sku_contract_violation"]
    assert violation["sku"] == hanging
    assert violation["log_level"] == "error"
    _assert_accounted(summary)
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_outer_cancel_cancels_all_workers():
    fetcher = _StubFetcher(delay_s=LONG_S)
    config = dataclasses.replace(CONFIG, deadline_s=LONG_S * 2)
    task = asyncio.create_task(
        run_pipeline(fetcher, _skus(100), config=config, emit=lambda _: None, stop=asyncio.Event()),
    )
    async with asyncio.timeout(1):
        while fetcher.in_flight < CONFIG.workers:  # noqa: ASYNC110 — ждём счётчик стаба, события у него нет
            await asyncio.sleep(0.001)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert len(fetcher.cancelled) == CONFIG.workers
    _assert_no_leaked_tasks()


@pytest.mark.asyncio
async def test_failing_input_fails_the_whole_run():
    def broken_skus() -> Iterator[str]:
        yield "SKU-00000"
        raise RuntimeError("input is corrupted")

    with pytest.RaisesGroup(RuntimeError):
        await run_pipeline(_StubFetcher(), broken_skus(), config=CONFIG, emit=lambda _: None, stop=asyncio.Event())

    _assert_no_leaked_tasks()


def _summary(**overrides: object) -> RunSummary:
    fields: dict[str, object] = {
        "enqueued": 10,
        "processed": 6,
        "not_started": 2,
        "cancelled": 0,
        "errors": 1,
        "contract_violations": 1,
        "statuses": {"ok": 6},
        "elapsed_s": 1.0,
        "shutdown": ShutdownMode.GRACEFUL,
    }
    return RunSummary(**(fields | overrides))  # type: ignore[arg-type]


def test_run_summary_accepts_consistent_counters():
    assert _summary().enqueued == 10


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"enqueued": 11}, id="unaccounted-sku"),
        pytest.param({"processed": 7}, id="double-counted-sku"),
        pytest.param({"processed": -1, "enqueued": 3}, id="negative-counter"),
        pytest.param({"shutdown": ShutdownMode.COMPLETED}, id="completed-with-dropped"),
        pytest.param({"cancelled": 1, "enqueued": 11}, id="cancelled-without-forced"),
    ],
)
def test_run_summary_rejects_impossible_states(overrides: dict[str, object]):
    with pytest.raises(ValueError):  # noqa: PT011 — сообщения разные, важен сам отказ
        _summary(**overrides)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"sources": ()}, id="no-sources"),
        pytest.param({"sources": ("alpha", "alpha")}, id="duplicate-sources"),
        pytest.param({"workers": 0}, id="no-workers"),
        pytest.param({"queue_size": 0}, id="unbounded-queue"),
        pytest.param({"deadline_s": 0}, id="zero-deadline"),
        pytest.param({"deadline_s": float("inf")}, id="infinite-deadline"),
        pytest.param({"grace_period_s": -1}, id="negative-grace"),
        pytest.param({"watchdog_margin_s": 0}, id="no-watchdog-margin"),
    ],
)
def test_pipeline_config_rejects_invalid_values(overrides: dict[str, object]):
    with pytest.raises(ValueError):  # noqa: PT011 — сообщения разные, важен сам отказ
        dataclasses.replace(CONFIG, **overrides)  # type: ignore[arg-type]
