# -*- coding: utf-8 -*-
import cost_effectiveness_analysis.model_client as model_client_module
from cost_effectiveness_analysis.model_client import CostEffectivenessModelClient


def test_failed_batch_is_bisected_and_only_bad_scheme_fails(monkeypatch):
    calls = []

    def fake_request(url, payload=None, timeout=1):
        ids = [item["candidate_id"] for item in payload["items"]]
        calls.append((url, ids))
        if "predict" in url and "bad" in ids:
            raise RuntimeError("价格模型缺少必填字段attr_006")
        if "predict" in url:
            return {"items":[{"candidate_id":x,"success":True,
                              "prediction":{"predicted_price_wan":10}} for x in ids],
                    "model":{"model_version":"p1"}}
        return {"items":[{"candidate_id":x,"success":True,
                           "evaluation":{"capability_score":90}} for x in ids],
                "model":{"model_version":"e1"}}

    monkeypatch.setattr(model_client_module, "_json_request", fake_request)
    client = CostEffectivenessModelClient("http://price", "http://effect", 1)
    schemes = [{"scheme_id":x,"model_parameters":{}} for x in ("good-a", "bad", "good-b")]
    result = client.evaluate_batch(schemes)
    rows = dict((item["scheme_id"], item) for item in result["items"])
    assert rows["good-a"]["predicted_price_wan"] == 10
    assert rows["good-b"]["predicted_price_wan"] == 10
    assert rows["bad"]["predicted_price_wan"] is None
    assert "attr_006" in rows["bad"]["price_error"]
    assert calls[0][1] == ["good-a", "bad", "good-b"]


def test_price_output_uses_independent_unit_and_scale(monkeypatch):
    def fake_request(url, payload=None, timeout=1):
        candidate_id = payload["items"][0]["candidate_id"]
        if "predict" in url:
            return {"items": [{"candidate_id": candidate_id, "success": True,
                               "prediction": {"predicted_price_wan": 250000}}]}
        return {"items": [{"candidate_id": candidate_id, "success": True,
                           "evaluation": {"capability_score": 100}}]}

    monkeypatch.setattr(model_client_module, "_json_request", fake_request)
    client = CostEffectivenessModelClient(
        "http://price", "http://effect", 1, {"unit": "yuan", "scale": 2}
    )
    result = client.evaluate_batch([{"scheme_id": "S1", "model_parameters": {}}])
    row = result["items"][0]
    assert row["predicted_price_wan"] == 50
    assert row["price_output_normalization"]["raw_value"] == 250000
    assert result["price_output"]["unit"] == "yuan"
    assert result["price_output"]["sample_wan"] == 24
