import pytest

from aggregator_app.aggregator import calc_best_offer
from aggregator_app.models import Offer

SKU_NAME = "sku-01"
CURRENCY_NAME = "RUB"
SIGNATURE = "signature"


@pytest.mark.parametrize(
    ("offers_data", "expected_best_offer_source"),
    [
        pytest.param([], None, id="empty-offers-list"),
        pytest.param(
            [
                Offer(source="alpha", sku=SKU_NAME, price_kopecks=200, currency=CURRENCY_NAME, signature=SIGNATURE),
                Offer(source="beta", sku=SKU_NAME, price_kopecks=100, currency=CURRENCY_NAME, signature=SIGNATURE),
                Offer(source="gamma", sku=SKU_NAME, price_kopecks=150, currency=CURRENCY_NAME, signature=SIGNATURE),
            ],
            "beta",
            id="offers-list-with-unique-prices",
        ),
        pytest.param(
            [
                Offer(source="alpha", sku=SKU_NAME, price_kopecks=200, currency=CURRENCY_NAME, signature=SIGNATURE),
                Offer(source="zeta", sku=SKU_NAME, price_kopecks=100, currency=CURRENCY_NAME, signature=SIGNATURE),
                Offer(source="gamma", sku=SKU_NAME, price_kopecks=150, currency=CURRENCY_NAME, signature=SIGNATURE),
                Offer(source="beta", sku=SKU_NAME, price_kopecks=100, currency=CURRENCY_NAME, signature=SIGNATURE),
            ],
            "beta",
            id="offers-list-with-few-min-prices",
        ),
    ],
)
def test_calc_best_offer(offers_data: list[Offer], expected_best_offer_source: str | None):
    if expected_best_offer_source is None:
        assert calc_best_offer(offers_data) is None
    else:
        assert calc_best_offer(offers_data).source == expected_best_offer_source
