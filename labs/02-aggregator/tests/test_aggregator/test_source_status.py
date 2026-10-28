import pytest

from aggregator_app.aggregator import source_status
from aggregator_app.models import Failed, FailReason, Fetched, Offer

STATUS_BY_REASON = {
    FailReason.TIMEOUT: "timeout",
    FailReason.TRANSPORT_ERROR: "transport_error",
    FailReason.INVALID_RESPONSE: "invalid_response",
    FailReason.BAD_SIGNATURE: "bad_signature",
}


def test_fetched_is_ok():
    offer = Offer(source="alpha", sku="sku-01", price_kopecks=10, currency="RUB", signature="sig")
    assert source_status(Fetched(offer=offer, attempts=1, elapsed_s=0.1)) == "ok"


@pytest.mark.parametrize("status_code", [404, 500, 502, 503])
def test_http_error_status_includes_code(status_code):
    failed = Failed(reason=FailReason.HTTP_ERROR, attempts=1, elapsed_s=0.1, status_code=status_code)
    assert source_status(failed) == f"http_{status_code}"


@pytest.mark.parametrize(("reason", "expected"), STATUS_BY_REASON.items(), ids=str)
def test_failed_status_is_reason_literal(reason, expected):
    status = source_status(Failed(reason=reason, attempts=1, elapsed_s=0.1))

    assert status == expected
    assert type(status) is str


def test_status_table_covers_every_fail_reason():
    """Runtime-аналог assert_never: новая причина в FailReason роняет тест, пока её нет в таблице."""
    assert set(STATUS_BY_REASON) | {FailReason.HTTP_ERROR} == set(FailReason)
