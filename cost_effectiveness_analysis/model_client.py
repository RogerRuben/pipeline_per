# -*- coding: utf-8 -*-
from __future__ import print_function

import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.model_service_client import _json_request
from app.price_output import PriceOutputNormalizer


class CostEffectivenessModelClient(object):
    def __init__(self, price_url, effectiveness_url, timeout=30.0, price_output_config=None):
        self.price_url = price_url.rstrip("/")
        self.effectiveness_url = effectiveness_url.rstrip("/")
        self.timeout = float(timeout)
        self.price_normalizer = PriceOutputNormalizer(price_output_config)

    def health(self):
        def probe(name, url):
            try:
                payload = _json_request(url + "/health", timeout=min(self.timeout, 4.0))
                return name, {"status": "ok" if payload.get("status") in ("ok", "healthy") else "unavailable",
                              "model_version": payload.get("model_version"), "detail": payload}
            except Exception as exc:
                return name, {"status": "unavailable", "message": str(exc)}
        with ThreadPoolExecutor(max_workers=2) as pool:
            return dict(f.result() for f in [
                pool.submit(probe, "price", self.price_url),
                pool.submit(probe, "effectiveness", self.effectiveness_url),
            ])

    @staticmethod
    def _map_items(payload):
        return dict((str(item.get("candidate_id")), item) for item in (payload or {}).get("items", []))

    @staticmethod
    def _connection_failure(message):
        """Do not fan out retries when the service itself is unreachable."""
        text = str(message or "").lower()
        return any(marker in text for marker in (
            "urlopen error", "timed out", "timeout", "connection refused",
            "actively refused", "winerror 10061", "无法连接", "远程主机",
        ))

    def _batch_with_isolation(self, url, payload):
        """Use one batch normally; bisect only a validation-failed batch.

        The current model services reject an entire batch when one candidate is
        invalid. Recursive bisection preserves batch efficiency while ensuring
        that an incomplete legacy scheme does not erase valid peer results.
        """
        collected, item_errors, model = {}, {}, {}
        first_error = [None]

        def request(items):
            part = dict(payload)
            part["items"] = items
            try:
                response = _json_request(url, part, self.timeout)
            except Exception as exc:
                message = str(exc)
                if first_error[0] is None:
                    first_error[0] = message
                if len(items) <= 1 or self._connection_failure(message):
                    for item in items:
                        item_errors[str(item.get("candidate_id"))] = message
                    return
                middle = len(items) // 2
                request(items[:middle])
                request(items[middle:])
                return
            if not model:
                model.update(response.get("model") or {})
            returned = self._map_items(response)
            for item in items:
                candidate_id = str(item.get("candidate_id"))
                value = returned.get(candidate_id)
                if value is None:
                    item_errors[candidate_id] = "模型服务未返回该方案"
                elif not value.get("success", True):
                    item_errors[candidate_id] = value.get("message") or value.get("error") or "模型计算失败"
                else:
                    collected[candidate_id] = value

        request(list(payload.get("items") or []))
        return {
            "items": collected, "item_errors": item_errors, "model": model,
            "error": first_error[0] if first_error[0] and not collected else None,
        }

    def evaluate_batch(self, schemes, target_protocol=None):
        request_id = "CE-%s" % uuid.uuid4().hex[:16]
        payload = {
            "request_id": request_id,
            "items": [{"candidate_id": item["scheme_id"], "parameters": item["model_parameters"],
                       "target_protocol": target_protocol} for item in schemes],
        }
        if target_protocol not in (None, ""):
            payload["target_protocol"] = target_protocol
        calls = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = {
                "price": pool.submit(self._batch_with_isolation, self.price_url + "/api/v1/predict/batch", payload),
                "effectiveness": pool.submit(self._batch_with_isolation, self.effectiveness_url + "/api/v1/evaluate/batch", payload),
            }
            for name, future in futures.items():
                calls[name] = future.result()
        pmap = calls["price"]["items"]
        emap = calls["effectiveness"]["items"]
        results = []
        for scheme in schemes:
            scheme_id = scheme["scheme_id"]
            price_item, effect_item = pmap.get(scheme_id), emap.get(scheme_id)
            price_error = calls["price"]["item_errors"].get(scheme_id)
            effect_error = calls["effectiveness"]["item_errors"].get(scheme_id)
            if price_item and price_item.get("success", True):
                try:
                    normalized_price = self.price_normalizer.normalize_response(price_item)
                    prediction = normalized_price.get("prediction") or {}
                    predicted = prediction.get("predicted_price_wan")
                    price_normalization = prediction.get("price_output_normalization")
                except (TypeError, ValueError) as exc:
                    predicted, price_normalization = None, None
                    price_error = "价格服务输出无法换算：%s" % exc
            else:
                predicted, price_normalization = None, None
                price_error = price_error or (price_item or {}).get("message") or "价格服务未返回该方案"
            if effect_item and effect_item.get("success", True):
                evaluation = effect_item.get("evaluation") or {}
                capability = evaluation.get("capability_score", evaluation.get("effectiveness_score"))
            else:
                evaluation, capability = {}, None
                effect_error = effect_error or (effect_item or {}).get("message") or "效能服务未返回该方案"
            results.append({
                "scheme_id": scheme_id, "predicted_price_wan": predicted,
                "price_output_normalization": price_normalization,
                "capability_score": capability,
                "feasibility_probability": evaluation.get("feasibility_probability"),
                "physical_gate": (effect_item or {}).get("physical_gate"),
                "risk_contributors": (effect_item or {}).get("risk_contributors") or [],
                "capability_contributors": (effect_item or {}).get("capability_contributors") or [],
                "price_error": price_error, "effectiveness_error": effect_error,
            })
        price_model = calls["price"]["model"]
        effect_model = calls["effectiveness"]["model"]
        protocol = next(((emap.get(x["scheme_id"]) or {}).get("protocol") for x in schemes
                         if (emap.get(x["scheme_id"]) or {}).get("protocol")), target_protocol)
        return {
            "items": results,
            "models": {"price_model_version": price_model.get("model_version"),
                       "effectiveness_model_version": effect_model.get("model_version")},
            "target_protocol": protocol, "analysis_time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "price_output": self.price_normalizer.describe(),
            "service_errors": {k: v["error"] for k, v in calls.items() if v["error"]},
        }
