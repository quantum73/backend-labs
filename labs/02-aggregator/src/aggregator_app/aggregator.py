import asyncio
import math
from asyncio import TaskGroup
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, assert_never

from aggregator_app.models import Failed, FailReason, Fetched, Offer, SourceResult

MS_PER_SECOND = 1000


class OfferFetcher(Protocol):
    """
    Источник предложений. Контракт, на который опирается aggregate_sku:
    - результат возвращается не позже deadline. deadline — абсолютное время в единицах loop.time();
    - сетевые проблемы — это значение Failed, а не исключение. Единственное допустимое исключение — CancelledError.
    Любое другое считается багом реализации;
    - Fetched означает: ответ разобран, source и sku совпали с запрошенными, подпись проверена.
      Агрегатор подпись повторно не проверяет.

    UpstreamClient соответствует протоколу структурно, без наследования.
    """

    async def fetch(self, source: str, sku: str, *, deadline: float) -> SourceResult: ...


type SourceStatus = str


@dataclass(frozen=True, slots=True)
class SkuResult:
    sku: str
    best: Offer | None
    offers_count: int
    sources: Mapping[str, SourceStatus]
    elapsed_ms: float

    def __post_init__(self) -> None:
        """
        frozen=True запрещает присвоить новое значение полю, но обычный dict внутри можно менять.
        Поэтому используем MappingProxyType, чтобы поле sources было read-only.
        Также явно делаем dict(self.sources), чтобы создался действительно новый объект в памяти.
        """
        if self.offers_count != len([status for status in self.sources.values() if status == "ok"]):
            raise ValueError("The `offers_count` differs from the `ok` values in `sources`.")
        if self.best is None and self.offers_count > 0:
            raise ValueError("`best` cannot be None if `offers_count` greater than 0")
        if self.best is not None and self.sku != self.best.sku:
            raise ValueError("`best` SKU must be equal result SKU")

        read_only_source = MappingProxyType(dict(self.sources))
        object.__setattr__(self, "sources", read_only_source)


def source_status(result: SourceResult) -> SourceStatus:
    match result:
        case Fetched():
            return "ok"
        case Failed(reason=FailReason.HTTP_ERROR) as http_fail:
            return f"http_{http_fail.status_code}"
        case Failed() as other_fail:
            return other_fail.reason.value
        case _ as unreachable:
            assert_never(unreachable)


def _validate_aggregate_sku_parameters(*, deadline_s: float, sources: Sequence[str]) -> None:
    if not math.isfinite(deadline_s):
        raise ValueError("`deadline_s` must be finite")
    if deadline_s <= 0:
        raise ValueError("`deadline_s` must be greater than 0")
    if isinstance(sources, str):
        raise ValueError("`sources` must be a sequence of names, not a string")  # noqa: TRY004 — один тип ошибки валидации
    if not sources:
        raise ValueError("`sources` cannot be empty")
    if len(sources) != len(set(sources)):
        raise ValueError("`sources` has duplicates")


def calc_best_offer(offers: Sequence[Offer]) -> Offer | None:
    """
    Детерминированный tie-break: При равной цене побеждает площадка, идущая первой по алфавиту.
    """
    if not offers:
        return None
    return min(offers, key=lambda offer: (offer.price_kopecks, offer.source))


async def aggregate_sku(
    fetcher: OfferFetcher,
    sku: str,
    *,
    sources: Sequence[str],
    deadline_s: float,
) -> SkuResult:
    """
    Опрашивает все площадки конкурентно и собирает лучшую цену и статус каждой площадки.

    За SLA SKU отвечает клиент. Агрегатор вычисляет один абсолютный дедлайн и передаёт его во все fetch,
    своего timeout_at не ставит.
    - Страховочный таймаут в агрегаторе создал бы два разных ``timeout``:
      «клиент честно не дождался ответа» и «агрегатор отменил задачу».
      Для потребителя они неразличимы, а для разбора инцидента различаются принципиально.
      К тому же у отменённой задачи нет результата.
    - Цена решения: реализация, нарушившая контракт (зависла дольше deadline), подвесит SKU.
      Страховка от этого — graceful-shutdown в будущих частях, где зависшие SKU отменяются целиком.

    Доказательство, что клиент соблюдает контракт:
    test_partial_result_with_hanging_source (настоящий клиент + фейковый транспорт).

    Исключения:
    - ValueError при невалидном входе (до запуска задач);
    - ExceptionGroup, если фетчер нарушил контракт и бросил исключение (баг не маскируется);
    - CancelledError при отмене.
    """

    _validate_aggregate_sku_parameters(deadline_s=deadline_s, sources=sources)

    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + deadline_s

    tasks: list[tuple[str, asyncio.Task[SourceResult]]] = []
    async with TaskGroup() as tg:
        for source in sources:
            fetch_task = tg.create_task(
                fetcher.fetch(source, sku, deadline=deadline),
                name=f"fetch:{source}:{sku}",
            )
            tasks.append((source, fetch_task))

    elapsed_ms = (loop.time() - started) * MS_PER_SECOND
    source_statuses: dict[str, SourceStatus] = {}
    offers = []
    for source, task in tasks:
        result = task.result()
        source_statuses[source] = source_status(result)
        if isinstance(result, Fetched):
            offers.append(result.offer)

    return SkuResult(
        sku=sku,
        best=calc_best_offer(offers),
        offers_count=len(offers),
        sources=source_statuses,
        elapsed_ms=elapsed_ms,
    )
