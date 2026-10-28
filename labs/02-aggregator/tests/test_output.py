import pytest

from aggregator_app.aggregator import SkuResult
from aggregator_app.models import Offer
from aggregator_app.output import sku_result_to_string

SOURCES = {"alpha": "ok", "beta": "ok", "gamma": "http_503", "delta": "timeout", "epsilon": "ok", "zeta": "ok"}
SOURCES_JSON = '{"alpha":"ok","beta":"ok","gamma":"http_503","delta":"timeout","epsilon":"ok","zeta":"ok"}'


def _result(*, sku: str = "SKU-00001", best: Offer | None = None, elapsed_ms: float = 512.3) -> SkuResult:
    return SkuResult(
        sku=sku,
        best=best,
        offers_count=4 if best else 0,
        sources=SOURCES,
        elapsed_ms=elapsed_ms,
    )


def test_golden_with_best_offer():
    best = Offer(
        source="alpha",
        sku="SKU-00001",
        price_kopecks=12345,
        currency="RUB",
        signature="secret-sig",
    )

    line = sku_result_to_string(_result(best=best))
    assert line == (
        '{"sku":"SKU-00001","best":{"source":"alpha","price_kopecks":12345,"currency":"RUB"},'
        f'"offers":4,"sources":{SOURCES_JSON},"elapsed_ms":512.3}}'
    )
    assert "secret-sig" not in line


def test_golden_without_best_offer():
    assert (
        sku_result_to_string(
            SkuResult(
                sku="SKU-00001",
                best=None,
                offers_count=0,
                sources={},
                elapsed_ms=512.3,
            )
        )
        == '{"sku":"SKU-00001","best":null,"offers":0,"sources":{},"elapsed_ms":512.3}'
    )


def test_non_ascii_is_kept_as_is():
    sku = "ЖЩЮ-1"
    assert sku_result_to_string(
        _result(
            sku=sku,
            best=Offer(
                source="alpha",
                sku=sku,
                price_kopecks=12345,
                currency="RUB",
                signature="secret-sig",
            ),
        )
    ).startswith('{"sku":"ЖЩЮ-1",')


def test_output_is_single_line():
    sku = "SKU\n1"
    assert "\n" not in sku_result_to_string(
        _result(
            sku=sku,
            best=Offer(
                source="alpha",
                sku=sku,
                price_kopecks=12345,
                currency="RUB",
                signature="secret-sig",
            ),
        )
    )


@pytest.mark.parametrize(
    ("elapsed_ms", "expected"),
    [
        (512.123456, "512.1"),
        (512.16, "512.2"),
        (0.04, "0.0"),
        (99.96, "100.0"),
    ],
)
def test_elapsed_ms_is_rounded_to_tenth(elapsed_ms, expected):
    best = Offer(
        source="alpha",
        sku="SKU-00001",
        price_kopecks=12345,
        currency="RUB",
        signature="secret-sig",
    )
    assert sku_result_to_string(_result(best=best, elapsed_ms=elapsed_ms)).endswith(f'"elapsed_ms":{expected}}}')
