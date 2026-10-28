import asyncio
import os
import random
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http import HTTPStatus
from types import TracebackType
from typing import Self

import httpx

from aggregator_app.models import Failed, FailReason, Fetched, Offer, SourceResult
from aggregator_app.retry import full_jitter, is_retryable_status, parse_retry_after
from aggregator_app.signatures import verify_signature
from aggregator_app.wire import InvalidOfferPayloadError, parse_offer

VERIFY_WORKERS_CAP = 4


def _default_verify_workers() -> int:
    return min(VERIFY_WORKERS_CAP, os.process_cpu_count() or 1)


@dataclass(frozen=True, slots=True)
class UpstreamConfig:
    """
    verify_workers — размер собственного пула проверки подписей:
    - pbkdf2_hmac отпускает GIL, поэтому потоки считают параллельно, но не больше, чем есть ядер:
      лишние потоки только стоят в очереди к CPU. Отсюда верхняя граница os.process_cpu_count();
    - Одна проверка ~4 мс (прогон на Apple M5 Pro). Даже если все max_concurrency=20 слотов отвечают
      за 30 мс (самая быстрая площадка), это ~670 ответов/с, то есть ~2.7 ядра.
      4 потока покрывают это с запасом (замер: 1 поток — 250 проверок/с, 4 — ~950/с);
    - больше 4 не берём: ядра нужны самому loop, резолверу DNS и остальному процессу.
    """

    max_concurrency: int = 20
    per_source_concurrency: int = 5
    max_attempts: int = 3
    backoff_base_s: float = 0.05
    backoff_cap_s: float = 0.5
    verify_workers: int = field(default_factory=_default_verify_workers)
    # Основной механизм — дедлайн, поэтому значения заметно больше SLA в 1с.
    # Таймауты ловят «тихо» умершее соединение, когда вызывающий передал очень длинный дедлайн.
    connect_timeout_s: float = 2.0
    io_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        positive_ints = {
            "max_concurrency": self.max_concurrency,
            "per_source_concurrency": self.per_source_concurrency,
            "max_attempts": self.max_attempts,
            "verify_workers": self.verify_workers,
        }
        positive_floats = {
            "backoff_base_s": self.backoff_base_s,
            "backoff_cap_s": self.backoff_cap_s,
            "connect_timeout_s": self.connect_timeout_s,
            "io_timeout_s": self.io_timeout_s,
        }
        errors = [
            f"`{name}` has to be greater than 0"
            for name, value in (positive_ints | positive_floats).items()
            if value <= 0
        ]
        if self.backoff_cap_s < self.backoff_base_s:
            errors.append("`backoff_cap_s` has to be >= `backoff_base_s`")
        if errors:
            raise ValueError("; ".join(errors))


@dataclass(frozen=True, slots=True)
class _AttemptFailure:
    reason: FailReason
    status_code: int | None = None
    retryable: bool = False
    retry_after_s: float | None = None


class UpstreamClient:
    def __init__(
        self,
        config: UpstreamConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,  # тесты подставляют фейк
        rng: random.Random | None = None,  # детерминированный jitter в тестах
    ) -> None:
        self._config = config
        self._transport = transport
        self._rng = rng if rng is not None else random.Random()  # noqa: S311 — jitter, не криптография
        self._global_slots = asyncio.Semaphore(config.max_concurrency)
        self._source_slots: dict[str, asyncio.Semaphore] = {}
        self._http: httpx.AsyncClient | None = None
        self._verify_pool: ThreadPoolExecutor | None = None

    async def __aenter__(self) -> Self:
        if self._http is not None:
            raise RuntimeError("UpstreamClient is already open")

        config = self._config
        self._verify_pool = ThreadPoolExecutor(
            max_workers=config.verify_workers,
            thread_name_prefix="verify-signature",
        )
        self._http = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(config.io_timeout_s, connect=config.connect_timeout_s),
            limits=httpx.Limits(
                max_connections=config.max_concurrency,
                max_keepalive_connections=config.max_concurrency,
            ),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        http, verify_pool = self._http, self._verify_pool
        self._http = None
        self._verify_pool = None
        try:
            if http is not None:
                await http.aclose()
        finally:
            if verify_pool is not None:
                # shutdown(wait=True) синхронный, поэтому ждём в потоке.
                # cancel_futures=True выбрасывает ещё не начатые проверки.
                await asyncio.to_thread(verify_pool.shutdown, wait=True, cancel_futures=True)

    def _require_open(self) -> tuple[httpx.AsyncClient, ThreadPoolExecutor]:
        if self._http is None or self._verify_pool is None:
            raise RuntimeError("UpstreamClient.fetch() must be called inside `async with UpstreamClient(...)`")

        return self._http, self._verify_pool

    def _source_slots_for(self, source: str) -> asyncio.Semaphore:
        slots = self._source_slots.get(source)
        if slots is None:
            slots = self._source_slots[source] = asyncio.Semaphore(self._config.per_source_concurrency)
        return slots

    @staticmethod
    def _check_response(response: httpx.Response, *, source: str, sku: str) -> Offer | _AttemptFailure:
        if not response.is_success:
            status = response.status_code
            retry_after_s = None
            if status == HTTPStatus.SERVICE_UNAVAILABLE:
                retry_after_s = parse_retry_after(response.headers.get("Retry-After"))

            return _AttemptFailure(
                FailReason.HTTP_ERROR,
                status_code=status,
                retryable=is_retryable_status(status),
                retry_after_s=retry_after_s,
            )

        try:
            offer = parse_offer(response.content)
        except InvalidOfferPayloadError:
            return _AttemptFailure(FailReason.INVALID_RESPONSE)

        if (offer.source, offer.sku) != (source, sku):
            return _AttemptFailure(FailReason.INVALID_RESPONSE)

        return offer

    def _retry_delay(
        self,
        failure: _AttemptFailure,
        attempt_no: int,
        *,
        now: float,
        deadline: float,
    ) -> float | None:
        if not failure.retryable or attempt_no >= self._config.max_attempts:
            return None

        if failure.retry_after_s is not None:
            delay = failure.retry_after_s
        else:
            delay = full_jitter(
                attempt=attempt_no,
                base_s=self._config.backoff_base_s,
                cap_s=self._config.backoff_cap_s,
                rng=self._rng,
            )

        return delay if now + delay < deadline else None

    async def fetch(self, source: str, sku: str, *, deadline: float) -> SourceResult:
        """
        Предложение площадки source по sku. Результат возвращается не позже deadline.
        :param deadline: абсолютное время в единицах ``loop.time()`` (монотонные часы).
        """

        http, verify_pool = self._require_open()
        loop = asyncio.get_running_loop()
        started = loop.time()
        source_slots = self._source_slots_for(source)
        attempts = 0  # отправленные запросы
        attempt_no = 0
        while True:
            attempt_no += 1
            if loop.time() >= deadline:
                return Failed(FailReason.TIMEOUT, attempts, loop.time() - started)

            budget = asyncio.timeout_at(deadline)
            try:
                async with budget:
                    async with source_slots, self._global_slots:
                        attempts += 1
                        response = await http.get(f"https://{source}.example/offers", params={"sku": sku})

                    outcome = self._check_response(response, source=source, sku=sku)
                    # Проверка подписи входит в дедлайн: дедлайн — это SLA, и ответ после него никому не нужен.
                    # Цена решения: отмена корутины не останавливает поток, поэтому уже начатая проверка (~4 мс)
                    # досчитается впустую. Ещё не начатые проверки снимаются из очереди пула вместе с отменой,
                    # так что потери ограничены verify_workers проверками.
                    if isinstance(outcome, Offer) and not await loop.run_in_executor(
                        verify_pool,
                        verify_signature,
                        outcome,
                    ):
                        outcome = _AttemptFailure(FailReason.BAD_SIGNATURE)

            except TimeoutError:
                if not budget.expired():
                    raise  # не наш дедлайн, не маскируем его под TIMEOUT

                return Failed(FailReason.TIMEOUT, attempts, loop.time() - started)
            except httpx.TransportError:  # включая httpx.TimeoutException от таймаутов
                outcome = _AttemptFailure(FailReason.TRANSPORT_ERROR, retryable=True)
            except httpx.DecodingError:  # битый/испорченный ответ
                outcome = _AttemptFailure(FailReason.INVALID_RESPONSE)

            if isinstance(outcome, Offer):
                return Fetched(outcome, attempts, loop.time() - started)

            delay = self._retry_delay(outcome, attempt_no, now=loop.time(), deadline=deadline)
            if delay is None:
                return Failed(outcome.reason, attempts, loop.time() - started, outcome.status_code)

            await asyncio.sleep(delay)
