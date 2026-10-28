import random

import pytest

from aggregator_app.retry import full_jitter, is_retryable_status, parse_retry_after


@pytest.mark.parametrize("seed", range(5))
@pytest.mark.parametrize("attempt", range(1, 7))
def test_full_jitter_stays_within_window(seed, attempt):
    base_s, cap_s = 0.05, 0.5
    rng = random.Random(seed)

    delays = [full_jitter(attempt=attempt, base_s=base_s, cap_s=cap_s, rng=rng) for _ in range(200)]

    assert all(0 <= delay <= min(cap_s, base_s * 2 ** (attempt - 1)) for delay in delays)


def test_full_jitter_is_deterministic_with_seed():
    def run() -> list[float]:
        rng = random.Random(42)
        return [full_jitter(attempt=n, base_s=0.05, cap_s=0.5, rng=rng) for n in range(1, 4)]

    assert run() == run()


def test_full_jitter_spreads_over_whole_window():
    rng = random.Random(0)
    delays = [full_jitter(attempt=3, base_s=0.05, cap_s=0.5, rng=rng) for _ in range(1000)]

    assert min(delays) < 0.02
    assert max(delays) > 0.18


def test_full_jitter_rejects_attempt_zero():
    with pytest.raises(ValueError, match="attempt"):
        full_jitter(attempt=0, base_s=0.05, cap_s=0.5, rng=random.Random(0))


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (502, True),
        (503, True),
        (504, True),
        (500, False),
        (404, False),
        (429, False),
    ],
)
def test_is_retryable_status(status_code, expected):
    assert is_retryable_status(status_code) is expected


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("2", 2.0),
        (" 0 ", 0.0),
        (None, None),
        ("", None),
        ("-1", None),
        ("1.5", None),
        ("Wed, 21 Oct 2026 07:28:00 GMT", None),
        ("٣", None),  # арабско-индийская цифра: isdecimal() её пропускает
    ],
)
def test_parse_retry_after(header, expected):
    assert parse_retry_after(header) == expected
