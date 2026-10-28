import json

import pytest

from aggregator_app.models import Offer
from aggregator_app.signatures import verify_signature
from aggregator_app.upstream import compute_signature
from aggregator_app.wire import InvalidOfferPayloadError, parse_offer


def _offer(**overrides: object) -> Offer:
    fields: dict = {"source": "alpha", "sku": "SKU-1", "price_kopecks": 12_345, "currency": "RUB"}
    fields["signature"] = compute_signature(fields["source"], fields["sku"], fields["price_kopecks"])
    return Offer(**(fields | overrides))


def test_valid_signature_matches_upstream():
    assert verify_signature(_offer())


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"price_kopecks": 1}, id="price-changed"),
        pytest.param({"sku": "SKU-2"}, id="sku-changed"),
        pytest.param({"signature": "0" * 64}, id="signature-forged"),
        pytest.param({"signature": ""}, id="signature-empty"),
    ],
)
def test_tampered_offer_fails_verification(overrides):
    assert not verify_signature(_offer(**overrides))


def _body(**overrides: object) -> bytes:
    payload = {"source": "alpha", "sku": "SKU-1", "price_kopecks": 100, "currency": "RUB", "signature": "ab"}
    return json.dumps(payload | overrides).encode()


def test_parse_offer_ignores_unknown_fields():
    assert parse_offer(_body(extra="new-field")) == Offer("alpha", "SKU-1", 100, "RUB", "ab")


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(b"not json", id="not-json"),
        pytest.param(b"[]", id="not-object"),
        pytest.param(_body(price_kopecks="100"), id="price-as-string"),
        pytest.param(_body(price_kopecks=100.0), id="price-as-float"),
        pytest.param(_body(price_kopecks=True), id="price-as-bool"),
        pytest.param(_body(price_kopecks=0), id="price-zero"),
        pytest.param(_body(price_kopecks=-1), id="price-negative"),
        pytest.param(_body(source=None), id="source-null"),
        pytest.param(json.dumps({"source": "alpha", "sku": "SKU-1"}).encode(), id="missing-fields"),
    ],
)
def test_parse_offer_rejects_malformed_payload(raw):
    with pytest.raises(InvalidOfferPayloadError) as exc_info:
        parse_offer(raw)
    assert exc_info.value.__cause__ is not None
