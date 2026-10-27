# -*- coding: utf-8 -*-
"""Canonical price-output contract at the model-service boundary.

Everything leaving :class:`ModelServiceGateway` under a ``*_wan`` name is in
ten-thousand yuan.  Historical business prices do not pass through this
normalizer.
"""
from __future__ import print_function

import hashlib
import json
import math


UNIT_TO_WAN = {
    "yuan": 0.0001,
    "thousand_yuan": 0.1,
    "wan_yuan": 1.0,
    "million_yuan": 100.0,
}

UNIT_LABELS = {
    "yuan": "元",
    "thousand_yuan": "千元",
    "wan_yuan": "万元",
    "million_yuan": "百万元",
}


def validate_price_output_config(config):
    item = dict(config or {})
    unit = str(item.get("unit") or "wan_yuan").strip().lower()
    if unit not in UNIT_TO_WAN:
        raise ValueError("价格服务输出单位无效：%s" % unit)
    try:
        scale = float(item.get("scale", 1.0))
    except (TypeError, ValueError):
        raise ValueError("价格服务输出尺度必须是有效数值")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("价格服务输出尺度必须是大于0的有限数值")
    return {"unit": unit, "scale": scale}


class PriceOutputNormalizer(object):
    VERSION = "price-output-normalization-v1"

    def __init__(self, config=None):
        self.config = validate_price_output_config(config)

    @property
    def factor_to_wan(self):
        return self.config["scale"] * UNIT_TO_WAN[self.config["unit"]]

    def normalize_value(self, raw):
        if raw in (None, ""):
            return None
        value = float(raw)
        if not math.isfinite(value):
            raise ValueError("价格服务返回了非有限数值")
        return value * self.factor_to_wan

    def normalize_interval(self, interval):
        if not isinstance(interval, (list, tuple)) or len(interval) < 2:
            return None
        return [self.normalize_value(interval[0]), self.normalize_value(interval[1])]

    def metadata(self, raw_value, normalized_value, raw_interval=None, normalized_interval=None):
        result = {
            "contract_version": self.VERSION,
            "signature": self.signature(),
            "raw_value": raw_value,
            "raw_unit": self.config["unit"],
            "raw_unit_label": UNIT_LABELS[self.config["unit"]],
            "scale": self.config["scale"],
            "unit_to_wan": UNIT_TO_WAN[self.config["unit"]],
            "normalized_unit": "wan_yuan",
            "normalized_value": normalized_value,
        }
        if raw_interval is not None:
            result["raw_interval"] = list(raw_interval)
        if normalized_interval is not None:
            result["normalized_interval_wan"] = list(normalized_interval)
        return result

    def describe(self):
        sample = 120000.0 if self.config["unit"] == "yuan" else 120.0 if self.config["unit"] == "thousand_yuan" else 12.0
        return {
            "unit": self.config["unit"],
            "unit_label": UNIT_LABELS[self.config["unit"]],
            "scale": self.config["scale"],
            "unit_to_wan": UNIT_TO_WAN[self.config["unit"]],
            "formula": "服务返回值 × 输出尺度 × 单位换算 = 系统万元价格",
            "sample_raw": sample,
            "sample_wan": self.normalize_value(sample),
            "contract_version": self.VERSION,
            "signature": self.signature(),
        }

    def signature(self):
        payload = {"version": self.VERSION, "unit": self.config["unit"], "scale": self.config["scale"]}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def normalize_response(self, response):
        result = dict(response or {})
        prediction = dict(result.get("prediction") or {})
        if "predicted_price_wan" not in prediction:
            return result
        raw_value = prediction.get("predicted_price_wan")
        normalized = self.normalize_value(raw_value)
        raw_interval = prediction.get("price_interval_wan")
        normalized_interval = self.normalize_interval(raw_interval) or [normalized, normalized]
        prediction["predicted_price_wan"] = normalized
        prediction["price_interval_wan"] = normalized_interval
        if prediction.get('recommendation_price_wan') is not None:
            prediction['recommendation_price_wan']=self.normalize_value(prediction['recommendation_price_wan'])
        prediction["price_output_normalization"] = self.metadata(
            raw_value, normalized, raw_interval=raw_interval, normalized_interval=normalized_interval
        )
        result["prediction"] = prediction
        result["price_output_normalization"] = dict(prediction["price_output_normalization"])
        return result
