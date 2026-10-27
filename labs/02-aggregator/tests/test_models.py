import pytest

from aggregator_app.models import Failed, FailReason, Fetched, Offer

OFFER = Offer(source="alpha", sku="SKU-1", price_kopecks=100, currency="RUB", signature="00")


@pytest.mark.parametrize(
    ("reason", "status_code"),
    [
        pytest.param(FailReason.HTTP_ERROR, None, id="http-error-without-status"),
        pytest.param(FailReason.TIMEOUT, 503, id="timeout-with-status"),
        pytest.param(FailReason.TRANSPORT_ERROR, 502, id="transport-error-with-status"),
        pytest.param(FailReason.BAD_SIGNATURE, 200, id="bad-signature-with-status"),
    ],
)
def test_failed_rejects_invalid_status_combination(reason, status_code):
    with pytest.raises(ValueError, match="status_code"):
        Failed(reason, attempts=1, elapsed_s=0.1, status_code=status_code)


@pytest.mark.parametrize(
    ("reason", "status_code"),
    [(FailReason.HTTP_ERROR, 404), (FailReason.TIMEOUT, None), (FailReason.INVALID_RESPONSE, None)],
)
def test_failed_accepts_valid_combination(reason, status_code):
    assert Failed(reason, attempts=1, elapsed_s=0.1, status_code=status_code).reason is reason


def test_failed_allows_zero_attempts_but_not_negative():
    assert Failed(FailReason.TIMEOUT, attempts=0, elapsed_s=0.1).attempts == 0
    with pytest.raises(ValueError, match="attempts"):
        Failed(FailReason.TIMEOUT, attempts=-1, elapsed_s=0.1)


def test_fetched_requires_sent_request():
    with pytest.raises(ValueError, match="at least one"):
        Fetched(OFFER, attempts=0, elapsed_s=0.1)


@pytest.mark.parametrize("price", [0, -1])
def test_offer_rejects_non_positive_price(price):
    with pytest.raises(ValueError, match="price_kopecks"):
        Offer(source="alpha", sku="SKU-1", price_kopecks=price, currency="RUB", signature="00")


def test_fail_reason_values_are_metric_friendly():
    assert [str(reason) for reason in FailReason] == [
        "timeout",
        "http_error",
        "transport_error",
        "invalid_response",
        "bad_signature",
    ]
