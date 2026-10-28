"""Конвейер: ленивый поток SKU → ограниченная очередь → N воркеров → emit(SkuResult).

run_pipeline не знает про сигналы, stdout и транспорт. Остановку, вывод и источник предложений
он получает параметрами (stop, emit, fetcher), поэтому целиком тестируется без подпроцессов.

Политика ошибок — изоляция SKU, а не fail fast:
- баг при обработке одного SKU (ExceptionGroup из aggregate_sku, ValueError из инвариантов SkuResult)
  логируется с трейсбеком (sku_failed), попадает в errors, и прогон продолжается.
  Остановка всего прогона из-за одного SKU превращает точечный баг в полный простой
  пакетной обработки: миллионы SKU ждут из-за одного;
- баг не исчезает молча: он в логе с трейсбеком, в errors и в коде выхода CLI (errors > 0 → 1);
- граница изоляции — один SKU. Ошибки самого конвейера (упал генератор skus, бросил emit) — это
  не «плохой SKU», а сломанная инфраструктура: TaskGroup отменяет остальных и пробрасывает ExceptionGroup.
"""

import asyncio
import contextlib
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum, auto
from types import MappingProxyType

import structlog

from aggregator_app.aggregator import OfferFetcher, SkuResult, aggregate_sku


class ShutdownMode(StrEnum):
    COMPLETED = auto()  # SKU закончились сами
    GRACEFUL = auto()  # пришёл stop, начатые SKU успели доработать
    FORCED = auto()  # пришёл stop, grace истёк или пришёл force, начатые SKU отменены


@dataclass(frozen=True, slots=True)
class PipelineConfig:
    """
    watchdog_margin_s — запас сверх deadline_s для страховочного таймаута вокруг aggregate_sku.
    По контракту SKU укладывается в deadline_s; срабатывание watchdog означает баг реализации, а не медленную площадку.
    0.1 с по умолчанию:
    - замеренный перебег дедлайна у клиента — доли миллисекунды, а целевой max loop lag — 50 мс:
      запас вдвое больше худшей допустимой задержки loop, ложных срабатываний не будет;
    - deadline_s + 0.1 = 1.1 с — это и есть целевое «время одного SKU» из SLA.
    """

    sources: tuple[str, ...]
    workers: int
    queue_size: int
    deadline_s: float
    grace_period_s: float
    watchdog_margin_s: float = 0.1

    def __post_init__(self) -> None:
        errors = []
        if not self.sources:
            errors.append("`sources` cannot be empty")
        if len(set(self.sources)) != len(self.sources):
            errors.append("`sources` has duplicates")
        if self.workers < 1:
            errors.append("`workers` has to be >= 1")
        if self.queue_size < 1:
            # asyncio.Queue(maxsize=0) — безлимитная очередь: backpressure пропал бы молча
            errors.append("`queue_size` has to be >= 1")
        if not (math.isfinite(self.deadline_s) and self.deadline_s > 0):
            errors.append("`deadline_s` has to be finite and > 0")
        if not (math.isfinite(self.grace_period_s) and self.grace_period_s >= 0):
            errors.append("`grace_period_s` has to be finite and >= 0")
        if not (math.isfinite(self.watchdog_margin_s) and self.watchdog_margin_s > 0):
            errors.append("`watchdog_margin_s` has to be finite and > 0")
        if errors:
            raise ValueError("; ".join(errors))


@dataclass(frozen=True, slots=True)
class RunSummary:
    enqueued: int  # SKU, попавшие в очередь
    processed: int  # выдан результат
    not_started: int  # были в очереди, но выброшены при остановке
    cancelled: int  # начаты, но отменены при FORCED
    errors: int  # баги при обработке SKU
    contract_violations: int  # сработал watchdog: aggregate_sku не уложился в deadline_s + запас
    statuses: Mapping[str, int]  # "ok" / "timeout" / "http_503" / … → количество по всем площадкам
    elapsed_s: float
    shutdown: ShutdownMode

    def __post_init__(self) -> None:
        counters = {
            "enqueued": self.enqueued,
            "processed": self.processed,
            "not_started": self.not_started,
            "cancelled": self.cancelled,
            "errors": self.errors,
            "contract_violations": self.contract_violations,
        }
        negative = [name for name, value in counters.items() if value < 0]
        if negative:
            raise ValueError(f"counters must be non-negative: {', '.join(negative)}")

        accounted = self.processed + self.not_started + self.cancelled + self.errors + self.contract_violations
        if self.enqueued != accounted:
            raise ValueError(f"every enqueued SKU must be accounted exactly once: {self.enqueued} != {accounted}")
        if self.shutdown is ShutdownMode.COMPLETED and (self.not_started or self.cancelled):
            raise ValueError("COMPLETED run cannot drop or cancel SKUs")
        if self.cancelled and self.shutdown is not ShutdownMode.FORCED:
            raise ValueError("only FORCED run can cancel started SKUs")

        object.__setattr__(self, "statuses", MappingProxyType(dict(self.statuses)))


async def run_pipeline(
    fetcher: OfferFetcher,
    skus: Iterable[str],
    *,
    config: PipelineConfig,
    emit: Callable[[SkuResult], None],
    stop: asyncio.Event,
    force: asyncio.Event | None = None,
) -> RunSummary:
    """
    Обрабатывает поток SKU не больше чем config.workers одновременно и отдаёт каждый результат в emit.

    :param skus: читается лениво; в памяти не больше queue_size + workers + 1 SKU (backpressure через очередь).
    :param emit: синхронный приёмник результата. Исключение из emit — сбой инфраструктуры, прогон падает целиком.
    :param stop: запрос остановки. Ещё не начатые SKU выбрасываются (not_started),
    начатые дорабатывают не дольше grace_period_s.
    :param force: запрос немедленной остановки во время grace (второй Ctrl+C): начатые SKU сразу отменяются.

    Режим остановки определяется по факту, а не по порядку событий, поэтому гонка «SKU закончились» и
    «пришёл stop» разрешается однозначно:
    - COMPLETED — вход исчерпан и каждый принятый SKU обработан, ничего не выброшено и не отменено.
      Так и при stop, пришедшем одновременно с последним SKU: работа не потеряна, значит, прогон завершён;
    - stop выставлен до вызова — skus не читается вовсе, результат GRACEFUL с нулевыми счётчиками;
    - GRACEFUL — был stop, начатые SKU доработали;
    - FORCED — был stop, начатые SKU не уложились в grace_period_s или пришёл force.

    Учёт: enqueued == processed + not_started + cancelled + errors + contract_violations (проверяет RunSummary).
    SKU, который producer уже взял из skus, но не успел положить в полную очередь до остановки,
    в enqueued не входит и нигде не учитывается: в очередь он не попал.
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    run = _Run(fetcher, config=config, emit=emit, stop=stop)
    if stop.is_set():
        return run.summary(ShutdownMode.GRACEFUL, elapsed_s=0.0)

    force = force if force is not None else asyncio.Event()
    forced = False
    async with asyncio.TaskGroup() as tg:
        tg.create_task(run.produce(skus), name="pipeline:producer")
        workers = {tg.create_task(run.work(), name=f"pipeline:worker-{n}") for n in range(1, config.workers + 1)}

        if not await _wait_all_or_event(workers, stop):
            run.drop_queued()
            with contextlib.suppress(TimeoutError):  # TimeoutError только от своего grace-таймаута
                async with asyncio.timeout(config.grace_period_s):
                    await _wait_all_or_event(workers, force)

            # Решаем по факту, а не по способу выхода: grace мог истечь ровно тогда, когда все доработали
            if not all(worker.done() for worker in workers):
                forced = True
                run.log_forced()
                for worker in workers:
                    worker.cancel()

                await asyncio.wait(workers)

    if forced:
        mode = ShutdownMode.FORCED
    elif run.input_exhausted and not run.not_started:
        mode = ShutdownMode.COMPLETED
    else:
        mode = ShutdownMode.GRACEFUL

    return run.summary(mode, elapsed_s=loop.time() - started)


async def _wait_all_or_event(tasks: set[asyncio.Task[None]], event: asyncio.Event) -> bool:
    """
    Ждёт завершения всех tasks. True — все завершились; False — раньше выставлено event.
    Ограничение по времени ставит вызывающий через asyncio.timeout: отмена корректно снимает waiter.
    """
    pending = {task for task in tasks if not task.done()}
    if not pending or event.is_set():
        return not pending

    # Владелец waiter — эта функция: он отменяется и дожидается в finally, наружу не утекает
    waiter = asyncio.create_task(_wait_event(event), name="pipeline:event-waiter")
    try:
        while pending and not event.is_set():
            _, pending = await asyncio.wait({*pending, waiter}, return_when=asyncio.FIRST_COMPLETED)
            pending.discard(waiter)
    finally:
        waiter.cancel()
        await asyncio.wait({waiter})  # не await waiter: его CancelledError неотличим от отмены нас самих

    return not pending


async def _wait_event(event: asyncio.Event) -> None:
    await event.wait()


class _Run:
    """Состояние одного прогона: очередь и счётчики. Всё в одном потоке loop, блокировки не нужны."""

    def __init__(
        self,
        fetcher: OfferFetcher,
        *,
        config: PipelineConfig,
        emit: Callable[[SkuResult], None],
        stop: asyncio.Event,
    ) -> None:
        self._fetcher = fetcher
        self._config = config
        self._emit = emit
        self._stop = stop
        self._queue: asyncio.Queue[str] = asyncio.Queue(maxsize=config.queue_size)
        self._logger = structlog.get_logger(__name__)
        self.input_exhausted = False
        self.in_progress = 0
        self.enqueued = 0
        self.processed = 0
        self.not_started = 0
        self.cancelled = 0
        self.errors = 0
        self.contract_violations = 0
        self.statuses: Counter[str] = Counter()

    async def produce(self, skus: Iterable[str]) -> None:
        try:
            for sku in skus:
                if self._stop.is_set():
                    return

                await self._queue.put(sku)
                self.enqueued += 1
        except asyncio.QueueShutDown:
            return  # остановка: очередь закрыта немедленно

        self.input_exhausted = True
        self._queue.shutdown()  # воркеры доработают остаток и выйдут

    async def work(self) -> None:
        while True:
            try:
                sku = await self._queue.get()
            except asyncio.QueueShutDown:
                return

            if self._stop.is_set():
                # stop уже выставлен, а очередь ещё не закрыта (run_pipeline не успел проснуться).
                # Новый SKU после stop не начинаем.
                self.not_started += 1
                continue

            self.in_progress += 1
            try:
                await self._process(sku)
            except asyncio.CancelledError:
                self.cancelled += 1
                raise
            finally:
                self.in_progress -= 1

    async def _process(self, sku: str) -> None:
        config = self._config
        watchdog_s = config.deadline_s + config.watchdog_margin_s
        watchdog = asyncio.timeout(watchdog_s)
        try:
            async with watchdog:
                result = await aggregate_sku(self._fetcher, sku, sources=config.sources, deadline_s=config.deadline_s)
        except Exception as exc:  # граница изоляции SKU, см. docstring модуля. CancelledError сюда не попадает
            if isinstance(exc, TimeoutError) and watchdog.expired():
                # Не статус площадки, а отдельный сигнал о баге. Трейсбек показывает await, на котором SKU завис.
                self.contract_violations += 1
                self._logger.exception("sku_contract_violation", sku=sku, watchdog_s=watchdog_s)
            else:
                self.errors += 1
                self._logger.exception("sku_failed", sku=sku)
            return

        self._emit(result)
        self.processed += 1
        self.statuses.update(result.sources.values())

    def drop_queued(self) -> None:
        # qsize и shutdown без await между ними: счётчик точный, никто не успеет взять SKU из очереди
        self.not_started += self._queue.qsize()
        self._queue.shutdown(immediate=True)
        # not_started включает и SKU, которые воркеры успели взять из очереди уже после stop
        self._logger.info("shutdown_started", not_started=self.not_started, in_progress=self.in_progress)

    def log_forced(self) -> None:
        self._logger.warning("shutdown_forced", in_progress=self.in_progress)

    def summary(self, mode: ShutdownMode, *, elapsed_s: float) -> RunSummary:
        return RunSummary(
            enqueued=self.enqueued,
            processed=self.processed,
            not_started=self.not_started,
            cancelled=self.cancelled,
            errors=self.errors,
            contract_violations=self.contract_violations,
            statuses=dict(sorted(self.statuses.items())),
            elapsed_s=elapsed_s,
            shutdown=mode,
        )
