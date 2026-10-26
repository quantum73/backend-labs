import asyncio
import contextlib
import time
from collections import deque
from collections.abc import Sequence

import pytest

from labkit.aio import PERCENTILE_MAX_VALUE, PERCENTILE_MIN_VALUE, LoopLagMonitor

INVALID_PERCENTILE_VALUE_ERROR_MESSAGE = (
    f"percentile `p` parameter has to be in \\[{PERCENTILE_MIN_VALUE}, {PERCENTILE_MAX_VALUE}]"
)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("interval_s", "max_samples", "expected_exception"),
    [
        (
            0,
            0,
            pytest.raises(
                ValueError,
                match="`interval_s` has to be greater than 0; `max_samples` has to be greater than 0",
            ),
        ),
        (-0.5, 10, pytest.raises(ValueError, match="`interval_s` has to be greater than 0")),
        (0.1, -5, pytest.raises(ValueError, match="`max_samples` has to be greater than 0")),
        (0.5, 10, contextlib.nullcontext()),
    ],
    ids=[
        "both_invalid",
        "interval_s_invalid",
        "max_samples_invalid",
        "both_valid",
    ],
)
async def test_loop_lag_monitor_parameters(
    interval_s: float,
    max_samples: int,
    expected_exception,
):
    """Тест валидации входных параметров."""
    with expected_exception:
        async with LoopLagMonitor(interval_s=interval_s, max_samples=max_samples) as monitor:
            assert monitor._interval_s == interval_s  # для тестов приемлимо проверить protected аргумент
            assert monitor._samples.maxlen == max_samples  # для тестов приемлимо проверить protected аргумент


@pytest.mark.parametrize(
    ("samples", "p", "expected_exception", "expected_result"),
    [
        ([], -10, pytest.raises(ValueError, match=INVALID_PERCENTILE_VALUE_ERROR_MESSAGE), None),
        ([], 100, pytest.raises(ValueError, match=INVALID_PERCENTILE_VALUE_ERROR_MESSAGE), None),
        ([], 110, pytest.raises(ValueError, match=INVALID_PERCENTILE_VALUE_ERROR_MESSAGE), None),
        ([], 0, pytest.raises(ValueError, match=INVALID_PERCENTILE_VALUE_ERROR_MESSAGE), None),
        ([], 90, contextlib.nullcontext(), 0.0),
        (list(range(1, 11)), 50, contextlib.nullcontext(), 5.5),
        (list(range(1, 11)), 25, contextlib.nullcontext(), 3.25),
        (list(range(1, 11)), 75, contextlib.nullcontext(), 7.75),
        (list(range(1, 11)), 1, contextlib.nullcontext(), 1.09),
        (list(range(1, 11)), 99, contextlib.nullcontext(), 9.91),
        ([5, 5, 5, 5], 50, contextlib.nullcontext(), 5.0),
        ([1, 1, 2, 2], 50, contextlib.nullcontext(), 1.5),
    ],
    ids=[
        "p_negative",
        "p_100",
        "p_over_100",
        "p_zero",
        "empty_samples",
        "median_1_10",
        "q1_10",
        "q3_10",
        "p_min_allowed",
        "p_max_allowed",
        "all_same",
        "duplicates",
    ],
)
def test_loop_lag_monitor_percentile(
    samples: Sequence[float],
    p: float,
    expected_exception,
    expected_result: float,
):
    """Тест на функцию расчёта перцентиля."""
    with expected_exception:
        result = LoopLagMonitor._percentile(samples=samples, p=p)
        assert result == pytest.approx(expected_result)


@pytest.mark.parametrize(
    ("p", "expected_exception"),
    [
        (0.9, pytest.raises(ValueError, match=INVALID_PERCENTILE_VALUE_ERROR_MESSAGE)),
        (99.1, pytest.raises(ValueError, match=INVALID_PERCENTILE_VALUE_ERROR_MESSAGE)),
        (1.0, contextlib.nullcontext()),
        (99.0, contextlib.nullcontext()),
    ],
    ids=["below_min", "above_max", "exact_min", "exact_max"],
)
def test_percentile_boundaries(p, expected_exception):
    """Тест на строгую валидацию границ [1, 99] параметра p."""
    with expected_exception:
        LoopLagMonitor._percentile(samples=[1, 2, 3], p=p)


@pytest.mark.parametrize("sequence_type", [list, tuple, deque], ids=["list", "tuple", "deque"])
def test_percentile_supports_sequence_interface(sequence_type):
    """Проверяем поддержку интерфейса Sequence на входе."""
    samples_sequence = sequence_type([10, 20, 30, 40, 50])
    result = LoopLagMonitor._percentile(samples=samples_sequence, p=50)
    assert result == pytest.approx(30.0)


@pytest.mark.asyncio
async def test_loop_lag_monitor():
    """
    Тест проверяет основную работу LoopLagMonitor:
    - Размер samples ограничен значением max_samples, старые значения вытесняются из буфера.
    - max_s считается за всё время, даже если сэмплы вытеснены из буфера.
    - Простой виден как простой: только await asyncio.sleep(0.2) → max_s < 0.02.
    """
    sleep_interval = 0.02
    async with LoopLagMonitor(interval_s=0.01, max_samples=1) as m:
        assert len(m.samples) == 0

        await asyncio.sleep(sleep_interval)
        assert m.max_s < sleep_interval
        assert len(m.samples) == 1

        await asyncio.sleep(sleep_interval)
        time.sleep(0.15)  # noqa: ASYNC251 — имитация задержек
        await asyncio.sleep(sleep_interval)
        assert m.max_s >= 0.15
        assert len(m.samples) == 1

        time.sleep(0.1)  # noqa: ASYNC251 — имитация задержек
        await asyncio.sleep(sleep_interval)
        assert m.max_s >= 0.15
        assert len(m.samples) == 1

        time.sleep(0.2)  # noqa: ASYNC251 — имитация задержек
        await asyncio.sleep(sleep_interval)
        assert m.max_s >= 0.2
        assert len(m.samples) == 1

    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.asyncio
async def test_loop_lag_monitor_leaks():
    """
    Тест проверяет что у LoopLagMonitor нет утечек, даже если внутри блока брошено исключение.
    Причём наружу вылетает именно это исключение.
    """

    async def _broken_logic():
        async with LoopLagMonitor(interval_s=0.01, max_samples=1) as m:
            assert len(m.samples) == 0

            await asyncio.sleep(0.02)
            assert m.max_s < 0.02
            assert len(m.samples) == 1

            raise ValueError("Broken!")

    with pytest.raises(ValueError, match="Broken!"):
        await _broken_logic()

    assert asyncio.all_tasks() == {asyncio.current_task()}


@pytest.mark.asyncio
async def test_loop_lag_monitor_is_one_time_use():
    async def _repeatable_call():
        monitor = LoopLagMonitor(interval_s=0.01, max_samples=1)
        async with monitor as m:
            assert m._used is True
            async with monitor:
                pass

    with pytest.raises(RuntimeError, match="Repeatable call\\. This operation is denied!"):
        await _repeatable_call()
