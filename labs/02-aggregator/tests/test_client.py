import asyncio
import contextlib
import json
import random
import threading
import time
from collections.abc import Callable

import httpx
import pytest

from aggregator_app.client import UpstreamClient, UpstreamConfig
from aggregator_app.models import Failed, FailReason, Fetched, Offer
from aggregator_app.upstream import FakeMarketplaceTransport, SourceProfile
from labkit.aio import LoopLagMonitor

pytestmark = pytest.mark.asyncio

SOURCES = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta")
FAST = SourceProfile(latency_s=0.01)
HANGING = SourceProfile(hang_rate=1.0)
NO_BACKOFF = {"backoff_base_s": 0.001, "backoff_cap_s": 0.001}
MONITOR_INTERVAL_S = 0.005


def _deadline(seconds: float) -> float:
    return asyncio.get_running_loop().time() + seconds


def _client(transport: httpx.AsyncBaseTransport, **config: float) -> UpstreamClient:
    return UpstreamClient(UpstreamConfig(**config), transport=transport, rng=random.Random(1))


async def _wait_until(predicate: Callable[[], bool]) -> None:
    async with asyncio.timeout(1):
        while not predicate():  # noqa: ASYNC110 — ждём счётчики фейка, событий у него нет
            await asyncio.sleep(0.001)


async def _let_monitor_record() -> None:
    # Без паузы монитор отменяется раньше, чем проснётся после блокировки, и не запишет её сэмпл
    await asyncio.sleep(MONITOR_INTERVAL_S * 4)


async def _cancel_all(tasks: list[asyncio.Task]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task


class _TamperingTransport(httpx.AsyncBaseTransport):
    """Пропускает ответ фейка через функцию порчи тела."""

    def __init__(self, inner: httpx.AsyncBaseTransport, tamper: Callable[[bytes], bytes]) -> None:
        self._inner = inner
        self._tamper = tamper

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        response = await self._inner.handle_async_request(request)
        body = await response.aread()
        return httpx.Response(response.status_code, content=self._tamper(body), request=request)


class _ScriptedTransport(httpx.AsyncBaseTransport):
    """Первые запросы отвечает по сценарию (ответ или исключение), остальные отдаёт фейку."""

    def __init__(
        self,
        inner: httpx.AsyncBaseTransport,
        script: list[Callable[[], httpx.Response] | Exception],
    ) -> None:
        self._inner = inner
        self._script = list(script)
        self.requests = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests += 1
        if not self._script:
            return await self._inner.handle_async_request(request)

        step = self._script.pop(0)
        if isinstance(step, Exception):
            raise step

        return step()


def _patch_json(**fields: object) -> Callable[[bytes], bytes]:
    return lambda body: json.dumps(json.loads(body) | fields).encode()


def _drop_field(name: str) -> Callable[[bytes], bytes]:
    return lambda body: json.dumps({k: v for k, v in json.loads(body).items() if k != name}).encode()


def _unavailable(retry_after: str | None = None) -> Callable[[], httpx.Response]:
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return lambda: httpx.Response(503, headers=headers)


async def test_success_returns_parsed_offer():
    transport = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    async with _client(transport) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    assert isinstance(result, Fetched)
    assert isinstance(result.offer, Offer)
    assert (result.offer.source, result.offer.sku, result.attempts) == ("alpha", "SKU-1", 1)
    assert transport.stats.requests_total["alpha"] == 1


async def test_retries_503_until_success():
    transport = FakeMarketplaceTransport({"alpha": SourceProfile(latency_s=0.01, fail_first=2)}, seed=1)
    async with _client(transport, **NO_BACKOFF) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    assert isinstance(result, Fetched)
    assert result.attempts == 3
    assert transport.stats.requests_total["alpha"] == 3


@pytest.mark.parametrize("status", [404, 500])
async def test_non_retryable_status_fails_immediately(status):
    transport = FakeMarketplaceTransport({"alpha": SourceProfile(latency_s=0.01, status=status)}, seed=1)
    async with _client(transport, **NO_BACKOFF) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    assert result == Failed(FailReason.HTTP_ERROR, attempts=1, elapsed_s=result.elapsed_s, status_code=status)
    assert transport.stats.requests_total["alpha"] == 1


async def test_hanging_source_times_out_at_deadline():
    transport = FakeMarketplaceTransport({"alpha": HANGING}, seed=1)
    async with _client(transport) as client:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await client.fetch("alpha", "SKU-1", deadline=started + 0.2)
        elapsed = loop.time() - started

    assert isinstance(result, Failed)
    assert (result.reason, result.attempts) == (FailReason.TIMEOUT, 1)
    assert elapsed <= 0.25
    assert transport.stats.cancelled["alpha"] == 1
    assert transport.stats.total_in_flight == 0


async def test_retries_do_not_cross_deadline():
    transport = FakeMarketplaceTransport({"alpha": SourceProfile(latency_s=0.01, error_rate=1.0)}, seed=1)
    async with _client(transport, backoff_base_s=10, backoff_cap_s=10) as client:
        deadline = _deadline(0.3)
        result = await client.fetch("alpha", "SKU-1", deadline=deadline)
        finished = asyncio.get_running_loop().time()

    assert isinstance(result, Failed)
    assert (result.reason, result.status_code) == (FailReason.HTTP_ERROR, 503)  # последняя реальная ошибка
    assert result.attempts < 3
    assert finished < deadline


async def test_slot_wait_counts_against_deadline():
    transport = FakeMarketplaceTransport({"alpha": HANGING}, seed=1)
    async with _client(transport, per_source_concurrency=1) as client:
        holder = asyncio.create_task(client.fetch("alpha", "SKU-1", deadline=_deadline(10)))
        await _wait_until(lambda: transport.stats.in_flight["alpha"] == 1)

        result = await client.fetch("alpha", "SKU-2", deadline=_deadline(0.1))

        await _cancel_all([holder])

    assert result == Failed(FailReason.TIMEOUT, attempts=0, elapsed_s=result.elapsed_s)
    assert transport.stats.requests_total["alpha"] == 1  # второй запрос так и не отправлен


async def test_concurrency_limits_are_respected():
    profiles = dict.fromkeys(SOURCES, SourceProfile(latency_s=0.02, jitter_s=0.01))
    transport = FakeMarketplaceTransport(profiles, seed=1)
    config = UpstreamConfig()
    async with UpstreamClient(config, transport=transport, rng=random.Random(1)) as client, asyncio.TaskGroup() as tg:
        tasks = [
            tg.create_task(client.fetch(source, f"SKU-{i}", deadline=_deadline(5)))
            for i in range(10)
            for source in SOURCES
        ]

    assert all(isinstance(task.result(), Fetched) for task in tasks)
    assert transport.stats.max_total_in_flight == config.max_concurrency  # лимит достигнут, но не превышен
    assert max(transport.stats.max_in_flight.values()) <= config.per_source_concurrency


async def test_hot_source_does_not_block_others():
    """Очередь к «горячей» площадке не должна занимать общие слоты."""
    transport = FakeMarketplaceTransport({"hot": HANGING, "cold": FAST}, seed=1)
    async with _client(transport, max_concurrency=4, per_source_concurrency=2) as client:
        hot = [asyncio.create_task(client.fetch("hot", f"SKU-{i}", deadline=_deadline(10))) for i in range(10)]
        await _wait_until(lambda: transport.stats.in_flight["hot"] == 2)

        loop = asyncio.get_running_loop()
        started = loop.time()
        async with asyncio.TaskGroup() as tg:
            cold = [tg.create_task(client.fetch("cold", f"SKU-{i}", deadline=_deadline(1))) for i in range(2)]

        elapsed = loop.time() - started
        await _cancel_all(hot)

    assert all(isinstance(task.result(), Fetched) for task in cold)
    assert elapsed < 0.2


@pytest.mark.parametrize(
    ("tamper", "reason"),
    [
        pytest.param(_patch_json(signature="0" * 64), FailReason.BAD_SIGNATURE, id="forged-signature"),
        pytest.param(_patch_json(price_kopecks=1), FailReason.BAD_SIGNATURE, id="price-changed"),
        pytest.param(lambda body: body[:-1], FailReason.INVALID_RESPONSE, id="broken-json"),
        pytest.param(_patch_json(sku="SKU-other"), FailReason.INVALID_RESPONSE, id="foreign-sku"),
        pytest.param(_patch_json(source="beta"), FailReason.INVALID_RESPONSE, id="foreign-source"),
        pytest.param(_patch_json(price_kopecks=-1), FailReason.INVALID_RESPONSE, id="negative-price"),
        pytest.param(_patch_json(price_kopecks="100"), FailReason.INVALID_RESPONSE, id="price-as-string"),
        pytest.param(_drop_field("currency"), FailReason.INVALID_RESPONSE, id="missing-field"),
    ],
)
async def test_tampered_response_is_rejected_without_retry(tamper, reason):
    inner = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    async with _client(_TamperingTransport(inner, tamper)) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    assert result == Failed(reason, attempts=1, elapsed_s=result.elapsed_s)
    assert inner.stats.requests_total["alpha"] == 1


async def test_cancel_mid_request_propagates_and_releases_slots():
    transport = FakeMarketplaceTransport({"alpha": HANGING}, seed=1)

    async with _client(transport, max_concurrency=1, per_source_concurrency=1) as client:
        task = asyncio.create_task(client.fetch("alpha", "SKU-1", deadline=_deadline(10)))
        await _wait_until(lambda: transport.stats.in_flight["alpha"] == 1)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # Слоты освобождены: следующий запрос отправляется сразу, а не ждёт слот до дедлайна
        after = await client.fetch("alpha", "SKU-2", deadline=_deadline(0.05))

    assert transport.stats.cancelled["alpha"] == 2
    assert transport.stats.total_in_flight == 0
    assert isinstance(after, Failed)
    assert after.attempts == 1
    assert asyncio.all_tasks() == {asyncio.current_task()}


async def test_loop_is_not_blocked_by_signature_checks():
    transport = FakeMarketplaceTransport(dict.fromkeys(SOURCES, FAST), seed=1)

    async with LoopLagMonitor(interval_s=MONITOR_INTERVAL_S) as monitor:
        async with _client(transport) as client, asyncio.TaskGroup() as tg:
            tasks = [tg.create_task(client.fetch(SOURCES[i % 6], f"SKU-{i}", deadline=_deadline(5))) for i in range(30)]
        await _let_monitor_record()

    assert all(isinstance(task.result(), Fetched) for task in tasks)
    assert monitor.max_s < 0.02


async def test_signature_check_counts_against_deadline(monkeypatch):
    def slow_verify(offer: Offer) -> bool:
        time.sleep(0.3)
        return True

    monkeypatch.setattr("aggregator_app.client.verify_signature", slow_verify)
    transport = FakeMarketplaceTransport({"alpha": FAST}, seed=1)

    async with _client(transport) as client:
        loop = asyncio.get_running_loop()
        started = loop.time()
        result = await client.fetch("alpha", "SKU-1", deadline=started + 0.1)
        elapsed = loop.time() - started

    assert result == Failed(FailReason.TIMEOUT, attempts=1, elapsed_s=result.elapsed_s)
    assert elapsed < 0.15


async def test_closing_waits_for_verify_pool_without_blocking_loop(monkeypatch):
    verify_started = threading.Event()

    def slow_verify(offer: Offer) -> bool:
        verify_started.set()
        time.sleep(0.2)
        return True

    monkeypatch.setattr("aggregator_app.client.verify_signature", slow_verify)
    transport = FakeMarketplaceTransport({"alpha": FAST}, seed=1)

    async with LoopLagMonitor(interval_s=MONITOR_INTERVAL_S) as monitor:
        async with _client(transport) as client:
            task = asyncio.create_task(client.fetch("alpha", "SKU-1", deadline=_deadline(5)))

            await _wait_until(verify_started.is_set)
            await _cancel_all([task])  # поток проверки продолжает работать: отмена его не останавливает

            loop = asyncio.get_running_loop()
            exit_started = loop.time()

        exit_took = loop.time() - exit_started
        await _let_monitor_record()

    assert exit_took >= 0.1  # __aexit__ дождался потока проверки
    assert monitor.max_s < 0.05  # но loop всё это время был свободен


async def test_lifecycle():
    transport = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    client = _client(transport)

    with pytest.raises(RuntimeError, match="inside `async with"):
        await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    async with client:
        http, verify_pool = client._http, client._verify_pool  # приемлемо для теста жизненного цикла
        with pytest.raises(RuntimeError, match="already open"):
            await client.__aenter__()

    assert http is not None
    assert http.is_closed
    assert verify_pool is not None

    with pytest.raises(RuntimeError, match="shutdown"):
        verify_pool.submit(print)

    with pytest.raises(RuntimeError, match="inside `async with"):
        await client.fetch("alpha", "SKU-1", deadline=_deadline(1))


async def test_retry_after_is_used_instead_of_jitter():
    inner = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    transport = _ScriptedTransport(inner, [_unavailable(retry_after="0")])
    # jitter с таким base не влез бы в дедлайн; повтор возможен только благодаря Retry-After
    async with _client(transport, backoff_base_s=10, backoff_cap_s=10) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    assert isinstance(result, Fetched)
    assert result.attempts == 2


async def test_retry_after_beyond_deadline_stops_retries():
    inner = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    transport = _ScriptedTransport(inner, [_unavailable(retry_after="5")])
    async with _client(transport, **NO_BACKOFF) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    assert result == Failed(FailReason.HTTP_ERROR, attempts=1, elapsed_s=result.elapsed_s, status_code=503)
    assert transport.requests == 1


@pytest.mark.parametrize(
    ("failures", "expected_type", "expected_attempts"),
    [
        pytest.param(2, Fetched, 3, id="recovers"),
        pytest.param(3, Failed, 3, id="exhausted"),
    ],
)
async def test_transport_errors_are_retried(failures, expected_type, expected_attempts):
    inner = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    transport = _ScriptedTransport(inner, [httpx.ConnectError("connection refused")] * failures)
    async with _client(transport, **NO_BACKOFF) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(1))

    assert isinstance(result, expected_type)
    assert result.attempts == expected_attempts
    if isinstance(result, Failed):
        assert result.reason is FailReason.TRANSPORT_ERROR


@pytest.mark.parametrize("bug", [KeyError("bug"), TypeError("bug"), TimeoutError("not our deadline")])
async def test_unexpected_exceptions_are_not_masked(bug):
    """Никакого `except Exception`: баг не должен превращаться в TRANSPORT_ERROR или TIMEOUT."""
    inner = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    async with _client(_ScriptedTransport(inner, [bug])) as client:
        with pytest.raises(type(bug)):
            await client.fetch("alpha", "SKU-1", deadline=_deadline(1))


async def test_expired_deadline_does_not_send_request():
    transport = FakeMarketplaceTransport({"alpha": FAST}, seed=1)
    async with _client(transport) as client:
        result = await client.fetch("alpha", "SKU-1", deadline=_deadline(-1))

    assert isinstance(result, Failed)
    assert result.reason is FailReason.TIMEOUT
    assert transport.stats.requests_total["alpha"] == 0
