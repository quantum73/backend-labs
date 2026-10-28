import pytest

from aggregator_app.aggregator import SkuResult


def test_sku_result_sources_field_as_read_only():
    obj = SkuResult(
        sku="sku-1",
        best=None,
        offers_count=1,
        sources={"alpha": "ok"},
        elapsed_ms=0.1,
    )

    with pytest.raises(TypeError):
        obj.sources["alpha"] = "http_503"
    with pytest.raises(TypeError):
        del obj.sources["alpha"]


def test_sku_result_sources_field_as_read_only_with_outside_declared_dict():
    sources_dict = {"alpha": "ok"}
    obj = SkuResult(
        sku="sku-1",
        best=None,
        offers_count=1,
        sources=sources_dict,
        elapsed_ms=0.1,
    )

    sources_dict["alpha"] = "timeout"
    sources_dict["beta"] = "ok"

    assert len(obj.sources) != len(sources_dict)
    assert obj.sources["alpha"] == "ok"
    assert obj.sources.get("beta") is None
