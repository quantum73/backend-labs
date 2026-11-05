"""
Запуск: uv run aggregator --skus 200 --seed 1 [--sources alpha,beta,gamma] > results.jsonl

Слой процесса: аргументы, настройки, сигналы, stdout и сборка зависимостей.
Результаты идут в stdout (JSON Lines), логи — в stderr.

Запись в stdout синхронная, без отдельной задачи-писателя (вариант (а) из 5.5):
- write блокирует loop, только когда переполнен буфер pipe (64 КБ на Linux), то есть потребитель не успевает
  читать. Это грубый, но честный backpressure: обгонять потребителя всё равно некуда;
- при ~15 строках/с по ~250 байт до этого далеко. Если запись всё же начнёт блокировать loop,
  это сразу видно в loop_lag_max_ms в run_finished;
- писатель через to_thread добавил бы ещё одну очередь, её размер, порядок строк и её drain при остановке.
  Сложность есть, выигрыша нет.

flush после каждой строки: системный вызов на результат при такой скорости ничего не стоит, зато потребитель
(`| jq`, `tail -f`) видит результаты сразу, а при SIGKILL теряется не больше одной строки.
"""

import argparse
import asyncio
import os
import random
import signal
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from typing import TextIO

import structlog
from pydantic import ValidationError

from aggregator_app.aggregator import SkuResult
from aggregator_app.client import UpstreamClient
from aggregator_app.output import sku_result_to_string
from aggregator_app.pipeline import RunSummary, ShutdownMode, run_pipeline
from aggregator_app.settings import AggregatorSettings
from aggregator_app.upstream import DEFAULT_PROFILES, FakeMarketplaceTransport
from labkit.aio import LoopLagMonitor
from labkit.logging import configure_logging
from labkit.settings import summarize_validation_errors

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT)
MS_PER_SECOND = 1000


def _positive_int(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {raw!r}") from None

    if value < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value}")

    return value


def _sources(raw: str) -> tuple[str, ...]:
    sources = tuple(name.strip() for name in raw.split(","))
    unknown = [name for name in sources if name not in DEFAULT_PROFILES]
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown sources {unknown}, known: {', '.join(DEFAULT_PROFILES)}")

    if len(set(sources)) != len(sources):
        raise argparse.ArgumentTypeError("sources must not repeat")

    return sources


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Offers aggregator: SKU results to stdout (JSON Lines), logs to stderr",
        epilog="Service behaviour (workers, limits, timeouts) is configured via APP_* environment variables.",
    )
    parser.add_argument(
        "--skus",
        type=_positive_int,
        required=True,
        help="number of SKUs to process",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="seed for the fake marketplaces and retry jitter",
    )
    parser.add_argument(
        "--sources",
        type=_sources,
        default=tuple(DEFAULT_PROFILES),
        help="comma-separated marketplaces (default: all)",
    )
    return parser.parse_args(argv)


def _skus(count: int) -> Iterator[str]:
    for n in range(count):
        yield f"SKU-{n:05d}"


class _StdoutSink:
    """
    emit для конвейера: одна строка JSON на результат.
    Закрытый потребитель (`| head -5`) — штатная остановка, а не трейсбек:
    on_closed выставляет stop, следующие результаты отбрасываются, а код выхода сообщает, что вывод неполный.
    """

    def __init__(self, stream: TextIO, *, on_closed: Callable[[], None]) -> None:
        self._stream = stream
        self._on_closed = on_closed
        self.closed = False

    def emit(self, result: SkuResult) -> None:
        if self.closed:
            return

        try:
            self._stream.write(sku_result_to_string(result) + "\n")
            self._stream.flush()
        except BrokenPipeError:
            self._close()

    def flush(self) -> None:
        if self.closed:
            return

        try:
            self._stream.flush()
        except BrokenPipeError:
            self._close()

    def _close(self) -> None:
        self.closed = True
        # Недописанное осталось в буфере, и при выходе Python сбросит его ещё раз: снова BrokenPipeError.
        # Подменяем дескриптор на /dev/null, буфер уйдёт туда.
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, self._stream.fileno())
        finally:
            os.close(devnull)
        self._on_closed()


@contextmanager
def _stop_on_signals(stop: asyncio.Event, force: asyncio.Event) -> Iterator[None]:
    """Первый SIGTERM/SIGINT — graceful (stop), второй во время grace — немедленный FORCED (force)."""
    loop = asyncio.get_running_loop()

    def on_signal() -> None:
        # Синхронный и минимальный: только выставить событие, всю работу делает конвейер
        if stop.is_set():
            force.set()
        else:
            stop.set()

    for sig in STOP_SIGNALS:
        loop.add_signal_handler(sig, on_signal)
    try:
        yield
    finally:
        for sig in STOP_SIGNALS:
            loop.remove_signal_handler(sig)


def _summary_fields(summary: RunSummary) -> dict[str, object]:
    return {
        "enqueued": summary.enqueued,
        "processed": summary.processed,
        "not_started": summary.not_started,
        "cancelled": summary.cancelled,
        "errors": summary.errors,
        "contract_violations": summary.contract_violations,
        "statuses": dict(summary.statuses),
        "elapsed_s": round(summary.elapsed_s, 3),
        "shutdown": summary.shutdown.value,
        "throughput_sku_per_s": round(summary.processed / summary.elapsed_s, 2) if summary.elapsed_s > 0 else 0.0,
    }


def _exit_code(summary: RunSummary, *, output_closed: bool) -> int:
    if summary.shutdown is ShutdownMode.FORCED or summary.errors or summary.contract_violations or output_closed:
        return EXIT_FAILURE
    return EXIT_OK


async def _run(args: argparse.Namespace, settings: AggregatorSettings) -> int:
    logger = structlog.get_logger()
    upstream_config = settings.to_upstream_config()
    pipeline_config = settings.to_pipeline_config(args.sources)
    stop = asyncio.Event()
    force = asyncio.Event()
    sink = _StdoutSink(sys.stdout, on_closed=stop.set)
    transport = FakeMarketplaceTransport(seed=args.seed)
    rng = random.Random(args.seed) if args.seed is not None else None  # noqa: S311 — jitter, не криптография

    with _stop_on_signals(stop, force):
        async with LoopLagMonitor() as lag, UpstreamClient(upstream_config, transport=transport, rng=rng) as client:
            logger.info(
                "run_started",
                skus=args.skus,
                seed=args.seed,
                upstream={
                    "max_concurrency": upstream_config.max_concurrency,
                    "per_source_concurrency": upstream_config.per_source_concurrency,
                    "max_attempts": upstream_config.max_attempts,
                    "verify_workers": upstream_config.verify_workers,
                },
                pipeline={
                    "sources": list(pipeline_config.sources),
                    "workers": pipeline_config.workers,
                    "queue_size": pipeline_config.queue_size,
                    "deadline_s": pipeline_config.deadline_s,
                    "grace_period_s": pipeline_config.grace_period_s,
                },
            )
            try:
                summary = await run_pipeline(
                    client,
                    _skus(args.skus),
                    config=pipeline_config,
                    emit=sink.emit,
                    stop=stop,
                    force=force,
                )
            finally:
                sink.flush()

    logger.info(
        "run_finished",
        **_summary_fields(summary),
        loop_lag_p99_ms=round(lag.percentile(99) * MS_PER_SECOND, 2),
        loop_lag_max_ms=round(lag.max_s * MS_PER_SECOND, 2),
        max_in_flight=transport.stats.max_total_in_flight,
        output_closed=sink.closed,
    )
    return _exit_code(summary, output_closed=sink.closed)


def _log_config_invalid(error: ValidationError) -> None:
    structlog.get_logger().error("config_invalid", errors=summarize_validation_errors(error))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        settings = AggregatorSettings()
    except ValidationError as error:
        configure_logging(level="ERROR", json=True, stream=sys.stderr)
        _log_config_invalid(error)
        return EXIT_USAGE

    configure_logging(level=settings.log_level, json=settings.log_json, stream=sys.stderr)
    return asyncio.run(_run(args, settings))


if __name__ == "__main__":
    raise SystemExit(main())
