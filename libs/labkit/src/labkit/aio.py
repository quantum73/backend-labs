import asyncio
import math
from collections import deque
from collections.abc import Sequence
from types import TracebackType
from typing import Self

PERCENTILE_MIN_VALUE = 0
PERCENTILE_MAX_VALUE = 100


class LoopLagMonitor:
    def __init__(self, *, interval_s: float = 0.01, max_samples: int = 100_000) -> None:
        """
        :param interval_s: Интервал проверок в секундах.
        Значение по умолчанию (0.01s / 10ms) — оптимальный компромисс:
        - Нагрузка на CPU мало заметна для приложения (<0.5%);
        - В среднем лаг, считается критическим, если превышает 50-100ms,
        поэтому текущий дефолт более чем с запасом будет ловить такие блокировки;
        - В современных ОС системный планировщик имеет шаг дискретизации времени около 1-15ms,
        поэтому нет смысла задавать interval_s равным или, тем более, меньшим чем шаг дискретизации.
        :param max_samples: Количество хранимых сэмплов, по умолчанию - 100_000.
        """
        self._validate_init_params(interval_s=interval_s, max_samples=max_samples)

        self._interval_s = interval_s
        self._max_s = 0.0
        self._samples: deque[float] = deque(maxlen=max_samples)
        self._task: asyncio.Task[None] | None = None
        self._used = False

    @classmethod
    def _validate_init_params(cls, *, interval_s: float, max_samples: int) -> None:
        errors = []
        if interval_s <= 0:
            errors.append("`interval_s` has to be greater than 0")
        if max_samples <= 0:
            errors.append("`max_samples` has to be greater than 0")

        if errors:
            full_error_message = "; ".join(errors).strip()
            raise ValueError(full_error_message)

    @property
    def samples(self) -> Sequence[float]:
        return tuple(self._samples)

    @property
    def max_s(self) -> float:
        return self._max_s

    @classmethod
    def _percentile(cls, *, samples: Sequence[float], p: float) -> float:
        """
        Линейная интерполяция между соседними порядковыми статистиками (метод 7 по Hyndman–Fan,
        как numpy.percentile по умолчанию и statistics.quantiles(method="inclusive")).
        p — любое число из [0, 100], включая дробные (p99.9); p=0 даёт минимум, p=100 — максимум.
        """
        if not PERCENTILE_MIN_VALUE <= p <= PERCENTILE_MAX_VALUE:
            raise ValueError(f"percentile `p` parameter has to be in [{PERCENTILE_MIN_VALUE}, {PERCENTILE_MAX_VALUE}]")

        if not samples:
            return 0.0

        ordered = sorted(samples)
        rank = p / 100 * (len(ordered) - 1)
        lower = math.floor(rank)
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (ordered[upper] - ordered[lower]) * (rank - lower)

    def percentile(self, p: float) -> float:
        return self._percentile(samples=self._samples, p=p)

    async def _run_monitor(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            started = loop.time()

            await asyncio.sleep(self._interval_s)

            lag_value = max(0.0, loop.time() - started - self._interval_s)
            self._samples.append(lag_value)
            self._max_s = max(self._max_s, lag_value)

    async def __aenter__(self) -> Self:
        if self._used:
            raise RuntimeError("Repeatable call. This operation is denied!")

        self._used = True
        self._task = asyncio.create_task(self._run_monitor(), name="loop-lag-monitor")
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: TracebackType | None,
    ) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                task = asyncio.current_task()
                if task is not None and task.cancelling():
                    raise
