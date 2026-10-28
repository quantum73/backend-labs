import pytest

from aggregator_app.aggregator import SkuResult


def test_sku_result_sources_field_as_read_only():
    obj = SkuResult(
        sku="sku-1",
        best=None,
        offers_count=0,
        sources={"alpha": "timeout"},
        elapsed_ms=0.1,
    )

    with pytest.raises(TypeError):
        obj.sources["alpha"] = "http_503"
    with pytest.raises(TypeError):
        del obj.sources["alpha"]


def test_sku_result_sources_field_as_read_only_with_outside_declared_dict():
    sources_dict = {"alpha": "timeout"}
    obj = SkuResult(
        sku="sku-1",
        best=None,
        offers_count=0,
        sources=sources_dict,
        elapsed_ms=0.1,
    )

    sources_dict["alpha"] = "ok"
    sources_dict["beta"] = "timeout"

    assert len(obj.sources) != len(sources_dict)
    assert obj.sources["alpha"] == "timeout"
    assert obj.sources.get("beta") is None
