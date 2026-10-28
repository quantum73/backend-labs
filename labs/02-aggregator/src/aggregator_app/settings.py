"""Настройки агрегатора: env (APP_*) → UpstreamConfig / PipelineConfig.

Конфиг и входные данные разделены:
- поведение сервиса (лимиты, таймауты, число воркеров, grace) приходит из env.
  Это свойства развёртывания: одинаковы для всех прогонов в окружении, задаются в манифесте и ревьюятся вместе с ним,
  меняются без пересборки образа;
- параметры конкретного прогона (--skus, --seed, --sources) приходят аргументами CLI.
  Это «что обработать», а не «как»: они разные у каждого запуска, и прятать их в env значит получить прогон,
  который нельзя воспроизвести по одной командной строке.

Настройки не уходят в домен: to_upstream_config() и to_pipeline_config() собирают обычные dataclass'ы.
Клиент и конвейер про env не знают.
"""

from collections.abc import Sequence
from typing import Self

from pydantic import Field, model_validator

from aggregator_app.client import UpstreamConfig
from aggregator_app.pipeline import PipelineConfig
from labkit.settings import AppSettings

# terminationGracePeriodSeconds в k8s по умолчанию: через 30 с после начала остановки под получает SIGKILL.
# В эти 30 с входит не только grace конвейера:
# - preStop-хук (обычно sleep 5–10 с, пока под убирают из балансировки) идёт ДО SIGTERM и съедает тот же бюджет;
# - после grace ещё нужно отменить воркеров, закрыть HTTP-клиент, дождаться пула проверки подписей,
#   сбросить stdout и записать run_finished.
# SIGKILL посреди этого — потерянная сводка и обрезанный вывод, поэтому оставляем не меньше 10 с запаса.
MAX_GRACE_PERIOD_S = 20.0


class AggregatorSettings(AppSettings):
    deadline_s: float = Field(default=1.0, gt=0, le=60)
    max_concurrency: int = Field(default=20, ge=1)
    per_source_concurrency: int = Field(default=5, ge=1)
    max_attempts: int = Field(default=3, ge=1, le=10)
    backoff_base_s: float = Field(default=0.05, gt=0)
    backoff_cap_s: float = Field(default=0.5, gt=0)
    verify_workers: int | None = Field(default=None, ge=1)  # None — значение по умолчанию UpstreamConfig
    # Объяснение через закон Литтла:
    # Узкое место - площадка epsilon: у неё 5 слотов, а запрос в среднем занимает 0.4 с.
    # Значит, она пропускает не больше 5 / 0.4 = 12.5 запросов в секунду, это потолок полных SKU.
    # Один SKU обрабатывается около 0.6 с, поэтому чтобы выйти на этот потолок, нужно 12.5 × 0.6 ≈ 7.5 воркеров.
    # Дальше лишние воркеры только стоят в очереди к epsilon, ожидание съедает дедлайн, и epsilon уходит в timeout.
    # Пропуская способность при этом растёт, но при этом SKU «заканчиваются» по дедлайну без самой медленной площадки.
    # В итоге полезная пропускная способность падает с 11.6 до 2.8, а лучшая цена может оказаться неверной.
    # Поэтому значение по умолчанию - 8.
    workers: int = Field(default=8, ge=1)
    queue_size: int = Field(default=100, ge=1)  # 0 в asyncio.Queue — безлимит, поэтому ge=1
    grace_period_s: float = Field(default=5.0, ge=0, le=MAX_GRACE_PERIOD_S)
    watchdog_margin_s: float = Field(default=0.1, gt=0)

    @model_validator(mode="after")
    def _check_cross_fields(self) -> Self:
        errors = []
        if self.per_source_concurrency > self.max_concurrency:
            errors.append("`per_source_concurrency` has to be <= `max_concurrency`")
        if self.backoff_cap_s < self.backoff_base_s:
            errors.append("`backoff_cap_s` has to be >= `backoff_base_s`")
        if self.grace_period_s < self.deadline_s + self.watchdog_margin_s:
            # grace короче жизни одного SKU: начатые SKU почти всегда будут отменены, GRACEFUL недостижим
            errors.append("`grace_period_s` has to be >= `deadline_s` + `watchdog_margin_s`")
        if errors:
            raise ValueError("; ".join(errors))

        # Инварианты dataclass'ов проверяются здесь, при старте, а не при первом вызове to_*_config()
        self.to_upstream_config()
        return self

    def to_upstream_config(self) -> UpstreamConfig:
        optional = {} if self.verify_workers is None else {"verify_workers": self.verify_workers}
        return UpstreamConfig(
            max_concurrency=self.max_concurrency,
            per_source_concurrency=self.per_source_concurrency,
            max_attempts=self.max_attempts,
            backoff_base_s=self.backoff_base_s,
            backoff_cap_s=self.backoff_cap_s,
            **optional,
        )

    def to_pipeline_config(self, sources: Sequence[str]) -> PipelineConfig:
        """sources — входные данные прогона (--sources), поэтому параметр, а не поле настроек."""
        return PipelineConfig(
            sources=tuple(sources),
            workers=self.workers,
            queue_size=self.queue_size,
            deadline_s=self.deadline_s,
            grace_period_s=self.grace_period_s,
            watchdog_margin_s=self.watchdog_margin_s,
        )
