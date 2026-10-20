# -*- coding: utf-8 -*-
"""HTTP model-service gateway and service-owned field catalog.

The recommendation system sends one complete parameters JSON to both services.
Each service owns field selection, parsing, preprocessing and model execution.
"""
from __future__ import print_function

import json
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request, build_opener, urlopen


class ModelServiceUnavailable(RuntimeError):
    pass


def _json_request(url, payload=None, timeout=15.0):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(url, data=data, headers={"Content-Type": "application/json", "Accept": "application/json"})
    try:
        host = (urlparse(url).hostname or "").lower()
        if host in ("127.0.0.1", "localhost", "::1"):
            response_context = build_opener(ProxyHandler({})).open(request, timeout=float(timeout))
        else:
            response_context = urlopen(request, timeout=float(timeout))
        with response_context as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
            message = detail.get("message") or detail.get("error") or str(exc)
        except Exception:
            message = str(exc)
        raise ModelServiceUnavailable("%s: %s" % (url, message))
    except (URLError, OSError, ValueError) as exc:
        raise ModelServiceUnavailable("%s: %s" % (url, exc))


class ModelServiceGateway(object):
    def __init__(self, local_runtime=None, price_url=None, effectiveness_url=None, timeout=15.0, fallback=False):
        self.local_runtime = local_runtime
        self.price_url = (price_url or os.environ.get("IPDEMO_PRICE_SERVICE_URL") or "http://127.0.0.1:18101").rstrip("/")
        self.effectiveness_url = (effectiveness_url or os.environ.get("IPDEMO_EFFECT_SERVICE_URL") or "http://127.0.0.1:18102").rstrip("/")
        self.timeout = float(timeout)
        self.fallback = bool(fallback)
        if self.fallback and self.local_runtime is None:
            raise ValueError("启用本地回退时必须提供本地模型运行时")
        self.product_code = self.local_runtime.schema.get("product_code") if self.local_runtime is not None else None
        self.last_status = {"mode": "not_called", "price": None, "effectiveness": None}

    def health(self):
        def probe(name, url):
            try:
                payload = _json_request(url + "/health", timeout=min(self.timeout, 4.0))
                payload.setdefault("url", url)
                return name, payload
            except Exception as exc:
                return name, {"status": "unavailable", "message": str(exc), "url": url}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(probe, name, url) for name, url in (
                ("price", self.price_url), ("effectiveness", self.effectiveness_url)
            )]
            return dict(future.result() for future in futures)

    def inspect_services(self):
        """Return independent health/schema diagnostics without pair validation."""
        services = dict((name, {"name": name, "url": url, "health_url": url + "/health",
                                "schema_url": url + "/api/v1/schema"}) for name, url in (
            ("price", self.price_url), ("effectiveness", self.effectiveness_url)
        ))
        def request_part(name, part):
            item = services[name]
            try:
                return name, part, _json_request(item[part + "_url"], timeout=min(self.timeout, 4.0)), None
            except Exception as exc:
                return name, part, None, str(exc)
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(request_part, name, part)
                       for name in services for part in ("health", "schema")]
            for future in futures:
                name, part, payload, error = future.result()
                if error and part == "health":
                    services[name]["health"] = {"status": "unavailable", "message": error}
                elif error:
                    services[name]["schema_error"] = error
                else:
                    services[name][part] = payload
        return services

    def schemas(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            p = pool.submit(_json_request, self.price_url + "/api/v1/schema", None, self.timeout)
            e = pool.submit(_json_request, self.effectiveness_url + "/api/v1/schema", None, self.timeout)
            result = {"price": p.result(), "effectiveness": e.result()}
        price_product = str(result["price"].get("product_code") or "")
        effect_product = str(result["effectiveness"].get("product_code") or "")
        # Schema is descriptive.  Recommendation readiness is proved by actual
        # prediction JSON, so a stale or incomplete schema must not make an
        # otherwise callable pair of HTTP services unavailable.
        self.product_code = price_product or effect_product or self.product_code
        return result

    def effectiveness_schema(self):
        """Inspect only the effectiveness service for its operator workbench."""
        return _json_request(
            self.effectiveness_url + "/api/v1/schema", None, self.timeout
        )

    def price_schema(self):
        """Inspect only the price service for the operator prediction page."""
        return _json_request(self.price_url + "/api/v1/schema", None, self.timeout)

    def predict_price(self, params, product_code=None):
        envelope = {
            "request_id": "PRICE-WORKBENCH-%s" % uuid.uuid4().hex[:16],
            "product_code": product_code or self.product_code,
            "scenario": "operator_price_workbench",
            "parameters": dict(params or {}),
            "context": {"source": "price_workbench", "locale": "zh-CN"},
        }
        return _json_request(self.price_url + "/api/v1/predict", envelope, self.timeout)

    def evaluate_effectiveness(self, params, target_protocol=None):
        """Evaluate one scheme without requiring the price service."""
        if target_protocol not in (None, ""):
            schema = self.effectiveness_schema()
            dynamic_protocol = bool(
                (schema.get("target_protocol_contract") or {}).get("supported")
                or (schema.get("capabilities") or {}).get("dynamic_target_protocol")
            )
            if not dynamic_protocol:
                target_protocol = None
        envelope = {
            "request_id": "EFFECT-WORKBENCH-%s" % uuid.uuid4().hex[:16],
            "product_code": None,
            "scenario": "operator_effectiveness_workbench",
            "parameters": dict(params or {}),
            "context": {"source": "effectiveness_workbench", "locale": "zh-CN"},
        }
        if target_protocol not in (None, ""):
            envelope["target_protocol"] = target_protocol
        return _json_request(
            self.effectiveness_url + "/api/v1/evaluate", envelope, self.timeout
        )

    def _local_evaluate(self, params, target_protocol=None):
        try:
            return self.local_runtime.evaluate(params, target_protocol=target_protocol)
        except TypeError:
            if target_protocol not in (None, ""):
                raise ModelServiceUnavailable("本地回退模型不支持动态目标协议")
            return self.local_runtime.evaluate(params)

    def evaluate(self, params, target_protocol=None):
        request_id = "REC-%s" % uuid.uuid4().hex[:16]
        envelope = {
            "request_id": request_id,
            "product_code": self.product_code,
            "scenario": "recommendation_evaluation",
            "parameters": dict(params or {}),
            "context": {"source": "industrial_recommendation_system", "locale": "zh-CN"},
        }
        if target_protocol not in (None, ""):
            envelope["target_protocol"] = target_protocol
        started = time.time()
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                price_future = pool.submit(_json_request, self.price_url + "/api/v1/predict", envelope, self.timeout)
                effect_future = pool.submit(_json_request, self.effectiveness_url + "/api/v1/evaluate", envelope, self.timeout)
                price = price_future.result()
                effect = effect_future.result()
            result = self._merge(envelope, price, effect)
            self.last_status = {"mode": "services", "elapsed_ms": round((time.time() - started) * 1000, 1), "price": price.get("model"), "effectiveness": effect.get("model")}
            return result
        except Exception as exc:
            if not self.fallback:
                raise
            result = self._local_evaluate(params, target_protocol=target_protocol)
            result["model_source"] = "local_fallback_after_service_error"
            result["service_error"] = str(exc)
            self.last_status = {"mode": "local_fallback", "elapsed_ms": round((time.time() - started) * 1000, 1), "error": str(exc)}
            return result

    def evaluate_batch(self, items, target_protocol=None):
        payload = {
            "request_id": "BATCH-%s" % uuid.uuid4().hex[:16],
            "product_code": self.product_code,
            "items": [
                {
                    "candidate_id": str(i.get("candidate_id") or index),
                    "parameters": dict(i.get("parameters") or i.get("params") or {}),
                    "target_protocol": i.get("target_protocol", target_protocol),
                }
                for index, i in enumerate(items)
            ],
        }
        if target_protocol not in (None, ""):
            payload["target_protocol"] = target_protocol
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                p = pool.submit(_json_request, self.price_url + "/api/v1/predict/batch", payload, self.timeout)
                e = pool.submit(_json_request, self.effectiveness_url + "/api/v1/evaluate/batch", payload, self.timeout)
                price, effect = p.result(), e.result()
            pmap = dict((str(x.get("candidate_id")), x) for x in price.get("items", []))
            emap = dict((str(x.get("candidate_id")), x) for x in effect.get("items", []))
            results = []
            for index, item in enumerate(items):
                candidate_id = str(item.get("candidate_id") or index)
                if candidate_id not in pmap or candidate_id not in emap:
                    raise ModelServiceUnavailable("批量模型服务缺少候选%s的返回结果" % candidate_id)
                envelope = {
                    "request_id": payload["request_id"],
                    "product_code": self.product_code,
                    "parameters": dict(item.get("parameters") or item.get("params") or {}),
                }
                item_protocol = item.get("target_protocol", target_protocol)
                if item_protocol not in (None, ""):
                    envelope["target_protocol"] = item_protocol
                results.append(self._merge(envelope, pmap[candidate_id], emap[candidate_id]))
            return results
        except Exception:
            if not self.fallback:
                raise
            return [
                self._local_evaluate(
                    item.get("parameters") or item.get("params") or {},
                    target_protocol=item.get("target_protocol", target_protocol),
                )
                for item in items
            ]

    def improve(self, params, target_protocol=None):
        envelope = {
            "request_id": "IMPROVE-%s" % uuid.uuid4().hex[:16],
            "product_code": self.product_code,
            "scenario": "counterfactual_improvement",
            "parameters": dict(params or {}),
            "context": {"source": "industrial_recommendation_system", "locale": "zh-CN"},
        }
        if target_protocol not in (None, ""):
            envelope["target_protocol"] = target_protocol
        response = _json_request(
            self.effectiveness_url + "/api/v1/improve",
            envelope,
            max(self.timeout, 45.0),
        )
        plan = dict(response.get("improvement_plan") or {})
        recommended = plan.get("recommended_parameters")
        if recommended:
            complete_recommended = dict(params or {})
            complete_recommended.update(recommended)
            plan["recommended_parameters"] = complete_recommended
            plan["recommended_evaluation"] = self.evaluate(
                complete_recommended,
                target_protocol=target_protocol,
            )
        return {
            "request_id": response.get("request_id"),
            "protocol": response.get("protocol"),
            "current": response.get("current") or {},
            "improvement_plan": plan,
            "model": response.get("model") or {},
        }

    @staticmethod
    def _merge(envelope, price, effect):
        prediction = price.get("prediction") or {}
        evaluation = effect.get("evaluation") or {}
        parameters = dict(envelope.get("parameters") or {})
        parameters.update(effect.get("parameters") or {})
        for key, item in (price.get("input_status") or {}).get("filled_fields", {}).items():
            parameters.setdefault(key, item.get("value"))
        price_value = float(prediction.get("predicted_price_wan"))
        ranking_price=max(price_value,float(prediction.get('recommendation_price_wan') or price_value))
        score = float(evaluation.get("effectiveness_score", evaluation.get("capability_score")))
        requirement = effect.get("requirement_assessment") or {}
        conservative_raw = evaluation.get("conservative_capability_score")
        if conservative_raw is None:
            conservative_raw = requirement.get("robust_p10")
        conservative_score = float(score if conservative_raw is None else conservative_raw)
        feasibility = float(evaluation.get("feasibility_probability"))
        domain_warnings = list((price.get("domain_status") or {}).get("warnings") or [])
        experience = list(effect.get("experience_extrapolations") or [])
        hard = list(effect.get("hard_violations") or [])
        physical_gate = dict(effect.get("physical_gate") or {})
        if not physical_gate:
            physical_gate = {
                "passed": not hard and feasibility >= 0.65,
                "decision": "pass" if not hard and feasibility >= 0.65 else "reject_hard_violation" if hard else "reject_low_feasibility_probability",
                "probability": feasibility,
                "probability_threshold": 0.65,
                "hard_violations": hard,
                "mature_boundary_violations": [],
                "severe_coupling_mismatches": [],
                "gate_policy": "gateway_compatibility_fallback",
            }
        gate_rejected = physical_gate.get("passed") is False
        combined_status = "out_of_domain" if hard or gate_rejected else "caution" if domain_warnings or experience else "in_domain"
        confidence = "low" if combined_status == "out_of_domain" else "medium" if combined_status == "caution" else "high"
        coupling = []
        for item in effect.get("coupling_assessments") or []:
            converted = dict(item)
            converted.setdefault("target", converted.get("target_key"))
            converted.setdefault("state", converted.get("status", "inside"))
            coupling.append(converted)
        interval = evaluation.get("protocol_score_interval") or requirement.get("protocol_score_interval")
        if not interval:
            interval = [conservative_score, score]
        uncertainty_width = evaluation.get("score_uncertainty_width")
        if uncertainty_width is None and isinstance(interval, (list, tuple)) and len(interval) >= 2:
            uncertainty_width = abs(float(interval[1]) - float(interval[0]))
        contributors = list(effect.get("capability_contributors") or [])
        if not contributors:
            for item in requirement.get("attributes") or []:
                contributors.append({
                    "parameter_id": item.get("attribute_key"),
                    "parameter_label": item.get("attribute_label"),
                    "unit": item.get("unit"),
                    "current_value": item.get("value"),
                    "reference_value": item.get("reference_value"),
                    "weight": item.get("weight"),
                    "relative_score_percent": item.get("relative_score_percent"),
                    "score_delta": item.get("score_delta"),
                    "explanation": item.get("explanation"),
                })
        return {
            "predicted_price_wan": price_value,
            'recommendation_price_wan':ranking_price,'price_risk':prediction.get('price_risk'),
            "price_interval_wan": prediction.get("price_interval_wan") or [price_value, price_value],
            "capability_score": score,
            "conservative_capability_score": conservative_score,
            "protocol_score_interval": interval,
            "support_at_80": evaluation.get("support_at_80", requirement.get("support_at_80")),
            "support_at_100": evaluation.get("support_at_100", requirement.get("support_at_100")),
            "robust_model_count": evaluation.get("robust_model_count", requirement.get("robust_model_count", 0)),
            "robust_unique_model_count": evaluation.get("robust_unique_model_count", requirement.get("robust_unique_model_count", 0)),
            "robust_conclusion": evaluation.get("robust_conclusion", requirement.get("robust_conclusion")),
            "robust_conclusion_label": evaluation.get("robust_conclusion_label", requirement.get("robust_conclusion_label")),
            "score_uncertainty_width": uncertainty_width,
            "cost_effectiveness": round(conservative_score / max(ranking_price, 1e-9), 4),
            "center_cost_effectiveness": round(score / max(price_value, 1e-9), 4),
            "feasibility_probability": feasibility,
            "feasibility_status": evaluation.get("feasibility_status"),
            "physical_gate": physical_gate,
            "prediction_confidence": confidence,
            "anomaly_assessment": {"status": combined_status, "is_anomaly": combined_status != "in_domain", "score": 1.0 - feasibility, "items": experience, "price_feature_anomalies": domain_warnings, "message": "模型服务评价完成。" if combined_status == "in_domain" else "至少一个模型服务报告边界、外推或硬风险。"},
            "effectiveness_anomaly_assessment": {"status": "caution" if experience else "in_domain", "is_anomaly": bool(experience), "items": experience},
            "price_anomaly_assessment": {"status": "caution" if domain_warnings else "in_domain", "is_anomaly": bool(domain_warnings), "items": domain_warnings},
            "price_imputed_features": [{"parameter_id": key, "policy": item.get("strategy"), "value": item.get("value")} for key, item in (price.get("input_status") or {}).get("filled_fields", {}).items()],
            "risk_contributors": effect.get("risk_contributors") or [],
            "hard_risk_reasons": [x.get("message", str(x)) if isinstance(x, dict) else str(x) for x in hard],
            "learned_boundary_violations": effect.get("learned_boundary_violations") or [],
            "coupling_assessments": coupling,
            "capability_contributors": contributors,
            "requirement_assessment": requirement,
            "protocol": effect.get("protocol"),
            "effectiveness_source": evaluation.get("effectiveness_source"),
            "model_versions": {"effectiveness": (effect.get("model") or {}).get("model_version"), "price": (price.get("model") or {}).get("model_version")},
            "model_audit": {"effectiveness": effect.get("model") or {}, "price": price.get("model") or {}},
            "model_source": "independent_http_model_services",
            "service_trace": {"request_id": envelope.get("request_id"), "price": price.get("model"), "effectiveness": effect.get("model")},
            "parameters": parameters,
        }


class _RemoteBundleView(object):
    def __init__(self, features=None, raw_contract=None, couplings=None, coupling_edges=None, learned_boundaries=None):
        self.features = list(features or [])
        self.raw_contract = list(raw_contract or [])
        self.couplings = list(couplings or [])
        self.coupling_edges = list(coupling_edges or [])
        self.learned_boundaries = list(learned_boundaries or [])
        self.by_key = dict((item.get("key"), item) for item in self.features)

    def coupling_band(self, model, params):
        target = model["target"]
        normalized = float(model.get("intercept", 0.0))
        for source in model.get("sources") or []:
            key = source.get("key")
            if key not in params:
                raise ValueError("耦合模型缺少源属性%s" % key)
            fallback = self.by_key.get(key) or {}
            lo, hi = (model.get("source_ranges") or {}).get(
                key,
                [fallback.get("min", 0.0), fallback.get("max", 1.0)],
            )
            z = (float(params[key]) - float(lo)) / max(float(hi) - float(lo), 1e-12)
            normalized += float(source.get("coefficient", 0.0)) * max(0.0, min(1.0, z))
        target_spec = self.by_key.get(target) or {}
        target_min = float(model.get("target_min", target_spec.get("min", 0.0)))
        target_max = float(model.get("target_max", target_spec.get("max", 1.0)))
        def denormalize(value):
            return target_min + float(value) * (target_max - target_min)
        predicted = denormalize(normalized)
        lower = denormalize(normalized + float(model.get("lower_offset", 0.0)))
        upper = denormalize(normalized + float(model.get("upper_offset", 0.0)))
        lower = max(target_min, min(target_max, lower))
        upper = max(target_min, min(target_max, upper))
        return {"predicted": predicted, "lower": min(lower, upper), "upper": max(lower, upper)}


def _remote_field(field):
    key = str(field.get("field_name") or field.get("key") or "").strip()
    if not key:
        raise ModelServiceUnavailable("模型服务Schema包含没有字段编号的字段")
    raw_type = str(field.get("dtype") or field.get("type") or "number").strip().lower()
    parser = field.get("parser")
    if raw_type in ("bool", "boolean"):
        value_type = "boolean"
    elif raw_type in ("enum", "categorical", "category", "string", "text"):
        value_type = "enum" if raw_type != "text" else "text"
    elif raw_type in ("ip", "ip_grade", "protection_grade"):
        value_type = "ip_grade"
        parser = parser or "ip_grade"
    else:
        value_type = "number"
    lower = field.get("generation_min", field.get("min"))
    upper = field.get("generation_max", field.get("max"))
    return {
        "key": key,
        "label": field.get("field_label") or field.get("label") or key,
        "unit": field.get("unit") or "",
        "type": value_type,
        "dtype": raw_type,
        "parser": parser,
        "min": lower,
        "max": upper,
        "training_min": field.get("training_min", lower),
        "training_max": field.get("training_max", upper),
        "training_mean": field.get("training_mean"),
        "default_value": field.get("default_value"),
        "required": bool(field.get("required", True)),
        "missing_policy": field.get("missing_policy") or "reject",
        "preference": field.get("preference_direction") or field.get("preference") or "neutral",
        "auto_adjustable": bool(field.get("participates_generation", True)),
        "default_visible": bool(field.get("default_visible", True)),
        "allowed_values": field.get("allowed_values") or field.get("categories"),
        "source": field.get("source") or field.get("source_type") or "product_parameter",
    }


class ServiceBackedRuntime(object):
    """Use service schemas as the contract and HTTP services for all predictions."""
    def __init__(self, gateway, schemas=None, local_runtime=None):
        self.gateway = gateway
        self.local_runtime = local_runtime
        self.schemas = schemas or gateway.schemas()
        price_schema = self.schemas["price"]
        effect_schema = self.schemas["effectiveness"]
        price_product = str(price_schema.get("product_code") or "")
        effect_product = str(effect_schema.get("product_code") or "")
        # Prefer an available declared code, but do not turn schema metadata
        # drift into a preflight gate.  Individual API responses remain the
        # source of truth for prediction payload shape.
        self.product_code = price_product or effect_product or gateway.product_code or ""
        gateway.product_code = self.product_code or None
        self.product_name = effect_schema.get("product_name") or price_schema.get("product_name") or price_product
        self._effect_features = [_remote_field(item) for item in effect_schema.get("fields") or []]
        self._price_features = [_remote_field(item) for item in price_schema.get("fields") or []]
        self.effectiveness = _RemoteBundleView(
            features=self._effect_features,
            couplings=effect_schema.get("coupling_models") or [],
            coupling_edges=effect_schema.get("coupling_edges") or [],
            learned_boundaries=effect_schema.get("learned_boundaries") or [],
        )
        self.price = _RemoteBundleView(raw_contract=self._price_features)
        self.dynamic_target_protocol_supported = bool(
            (effect_schema.get("target_protocol_contract") or {}).get("supported")
        )
        self.counterfactual_improvement_supported = bool(
            (effect_schema.get("capabilities") or {}).get("counterfactual_improvement")
        )
        self.contract_version = "service-schema-1.1"

    def _supported_target_protocol(self, target_protocol):
        """Only forward per-request protocols when the effectiveness service owns that capability.

        V10/original-runtime packages contain a fixed packaged protocol.  The UI and
        recommendation API may still carry the selected protocol identifier, but
        forwarding it would incorrectly turn a fixed-profile evaluation into a
        dynamic-profile request and the service must reject that request.
        """
        if not self.dynamic_target_protocol_supported:
            return None
        return target_protocol

    @property
    def schema(self):
        return {
            "product_code": self.product_code,
            "product_name": self.product_name,
            "features": list(self._effect_features),
        }

    def feature_roles(self):
        effect = set(item["key"] for item in self._effect_features)
        price = set(item["key"] for item in self._price_features if item.get("source", "product_parameter") == "product_parameter")
        return {
            "shared_features": sorted(effect & price),
            "effectiveness_only_features": sorted(effect - price),
            "price_only_features": sorted(price - effect),
        }

    def all_feature_specs(self):
        roles = self.feature_roles()
        shared = set(roles["shared_features"])
        result = []
        seen = set()
        for source in self._effect_features:
            item = dict(source)
            item["model_role"] = "shared" if item["key"] in shared else "effectiveness_only"
            item["default_visible"] = True
            result.append(item)
            seen.add(item["key"])
        for source in self._price_features:
            if source.get("source", "product_parameter") != "product_parameter" or source["key"] in seen:
                continue
            item = dict(source)
            item["model_role"] = "price_only"
            item["default_visible"] = False
            result.append(item)
            seen.add(item["key"])
        return result

    def evaluate(self, params, target_protocol=None):
        target_protocol = self._supported_target_protocol(target_protocol)
        try:
            return self.gateway.evaluate(params, target_protocol=target_protocol)
        except TypeError:
            if target_protocol not in (None, ""):
                raise
            return self.gateway.evaluate(params)

    def evaluate_batch(self, items, target_protocol=None):
        target_protocol = self._supported_target_protocol(target_protocol)
        if not self.dynamic_target_protocol_supported:
            items = [
                dict((key, value) for key, value in item.items() if key != "target_protocol")
                for item in items
            ]
        try:
            return self.gateway.evaluate_batch(items, target_protocol=target_protocol)
        except TypeError:
            if target_protocol not in (None, ""):
                raise
            return self.gateway.evaluate_batch(items)

    def improve(self, params, target_protocol=None):
        if not self.counterfactual_improvement_supported:
            return self._compatibility_improve(params, target_protocol=target_protocol)
        return self.gateway.improve(
            params,
            target_protocol=self._supported_target_protocol(target_protocol),
        )

    @staticmethod
    def _improvement_rank(evaluation):
        gate = evaluation.get("physical_gate") or {}
        gate_ok = 1 if gate.get("passed") is not False else 0
        conservative = evaluation.get("conservative_capability_score")
        if conservative is None:
            conservative = evaluation.get("capability_score") or 0.0
        return (
            gate_ok,
            float(evaluation.get("feasibility_probability") or 0.0),
            float(conservative),
            -float(evaluation.get("predicted_price_wan") or 0.0),
        )

    @staticmethod
    def _candidate_values(current, field):
        dtype = str(field.get("type") or field.get("dtype") or "number").lower()
        allowed = list(field.get("allowed_values") or [])
        if dtype == "boolean":
            return [1 - int(float(current))]
        if dtype in ("enum", "text") or allowed:
            return [value for value in allowed if str(value) != str(current)][:12]
        try:
            value = float(current)
        except (TypeError, ValueError):
            return []
        lower, upper = field.get("min"), field.get("max")
        if lower is None:
            lower = field.get("training_min")
        if upper is None:
            upper = field.get("training_max")
        if lower is None or upper is None:
            span = max(abs(value), 1.0)
            lower, upper = value - span, value + span
        lower, upper = float(lower), float(upper)
        span = max(upper - lower, 1e-9)
        values = [value - span * 0.12, value + span * 0.12, lower, upper]
        if dtype in ("integer", "ip_grade") or field.get("parser") == "ip_grade":
            values = [int(round(item)) for item in values]
        result = []
        for item in values:
            item = max(lower, min(upper, item))
            if dtype in ("integer", "ip_grade") or field.get("parser") == "ip_grade":
                item = int(round(item))
            if str(item) != str(current) and item not in result:
                result.append(item)
        return result

    def _compatibility_improve(self, params, target_protocol=None):
        """Model-agnostic batched neighborhood search for fixed V10 packages."""
        active_protocol = self._supported_target_protocol(target_protocol)
        base_params = dict(params or {})
        current = self.evaluate(base_params, target_protocol=active_protocol)
        best_params, best = dict(base_params), current
        evaluated_count = 1
        for _round in range(2):
            candidates, signatures = [], set()
            for field in self._effect_features:
                key = field["key"]
                if not field.get("auto_adjustable", True) or key not in best_params:
                    continue
                for value in self._candidate_values(best_params.get(key), field):
                    trial = dict(best_params)
                    trial[key] = value
                    signature = tuple((name, str(trial.get(name))) for name in sorted(trial))
                    if signature in signatures:
                        continue
                    signatures.add(signature)
                    candidates.append({"candidate_id": "IMPROVE-%03d" % len(candidates), "parameters": trial})
            if not candidates:
                break
            evaluations = self.evaluate_batch(candidates, target_protocol=active_protocol)
            evaluated_count += len(evaluations)
            winner = max(zip(candidates, evaluations), key=lambda item: self._improvement_rank(item[1]))
            if self._improvement_rank(winner[1]) <= self._improvement_rank(best):
                break
            best_params, best = dict(winner[0]["parameters"]), winner[1]

        before_score = float(current.get("conservative_capability_score", current.get("capability_score") or 0.0))
        after_score = float(best.get("conservative_capability_score", best.get("capability_score") or 0.0))
        fields = dict((item["key"], item) for item in self.all_feature_specs())
        changes = []
        for key in sorted(best_params):
            if str(best_params.get(key)) == str(base_params.get(key)):
                continue
            field = fields.get(key, {})
            changes.append({
                "parameter_id": key,
                "attribute_label": field.get("label", key),
                "before": base_params.get(key),
                "after": best_params.get(key),
                "unit": field.get("unit") or "",
                "score_gain": round(after_score - before_score, 6),
            })
        message = (
            "已用V10兼容批量邻域搜索找到改进方案。"
            if changes else
            "当前V10模型邻域内未找到更优且更可行的单项/两步调整。"
        )
        return {
            "request_id": "COMPAT-%s" % uuid.uuid4().hex[:12],
            "protocol": best.get("protocol") or current.get("protocol"),
            "current": {
                "capability_score": current.get("capability_score"),
                "conservative_capability_score": current.get("conservative_capability_score"),
                "feasibility_probability": current.get("feasibility_probability"),
                "physical_gate": current.get("physical_gate") or {},
            },
            "improvement_plan": {
                "message": message,
                "search_mode": "v10_service_compatibility_batched_neighborhood",
                "evaluated_candidate_count": evaluated_count,
                "before_score": before_score,
                "after_score": after_score,
                "changes": changes,
                "recommended_parameters": best_params if changes else {},
                "recommended_evaluation": best if changes else None,
            },
            "model": {
                "backend": (self.schemas.get("effectiveness") or {}).get("backend"),
                "compatibility_search": True,
            },
        }

    def manifest(self):
        roles = self.feature_roles()
        price = self.schemas["price"]
        effect = self.schemas["effectiveness"]
        warnings = []
        if not self._effect_features:
            warnings.append("效能服务Schema未声明字段；仍允许按业务成品参数调用API。")
        if not self._price_features:
            warnings.append("价格服务Schema未声明字段；仍允许按业务成品参数调用API。")
        return {
            "contract_version": self.contract_version,
            "supported_contract_versions": [self.contract_version],
            "contract_valid": True,
            "contract_warnings": warnings,
            "product_code": self.product_code,
            "schema_keys": [item["key"] for item in self.all_feature_specs()],
            "feature_compatibility": {
                "effectiveness_features": sorted(item["key"] for item in self._effect_features),
                "price_raw_features": sorted(item["key"] for item in self._price_features),
                "shared_features": roles["shared_features"],
                "effectiveness_only_features": roles["effectiveness_only_features"],
                "price_only_features": roles["price_only_features"],
                "policy": "service_schema_union",
            },
            "effectiveness": {
                "model_version": effect.get("model_version"),
                "product_code": self.product_code,
                "model_kind": "effectiveness",
                "backend": effect.get("backend"),
                "algorithm_version": effect.get("algorithm_version"),
                "profile_version": effect.get("profile_version"),
                "state_sha256": effect.get("state_sha256"),
                "active_protocol": effect.get("active_protocol"),
                "protocol_profiles": effect.get("protocol_profiles") or [],
                "target_protocol_contract": effect.get("target_protocol_contract") or {},
                "capabilities": effect.get("capabilities") or {},
                "coupling_model_count": len(effect.get("coupling_models") or []),
                "coupling_edge_count": len(effect.get("coupling_edges") or []),
                "learned_boundary_count": len(effect.get("learned_boundaries") or []),
                "artifact_sha256": effect.get("state_sha256") or effect.get("model_sha256") or effect.get("workbook_fingerprint") or effect.get("learning_fingerprint"),
            },
            "price": {
                "model_version": price.get("model_version"),
                "product_code": self.product_code,
                "model_kind": "price",
                "backend": price.get("backend"),
                "artifact_sha256": price.get("model_sha256"),
            },
            "execution_mode": "independent_http_services",
            "local_fallback_enabled": bool(self.gateway.fallback),
        }


class DegradedServiceRuntime(object):
    """Model-free runtime used only while independent HTTP services are down.

    It deliberately has no local prediction fallback.  The Store can still
    expose and maintain business data, and the recommendation layer can still
    rank stored historical products by attributes, tags and historical price.
    """
    def __init__(self, gateway, product_code="", product_name=""):
        self.gateway = gateway
        self.product_code = str(product_code or "")
        self.product_name = str(product_name or self.product_code)
        self.effectiveness = _RemoteBundleView(features=[])
        self.price = _RemoteBundleView(raw_contract=[])
        self.contract_version = "service-schema-unavailable"

    @property
    def schema(self):
        return {"product_code": self.product_code, "product_name": self.product_name, "features": []}

    def feature_roles(self):
        return {"shared_features": [], "effectiveness_only_features": [], "price_only_features": []}

    def all_feature_specs(self):
        return []

    def manifest(self):
        return {
            "contract_version": self.contract_version,
            "supported_contract_versions": [],
            "contract_valid": False,
            "contract_warnings": ["独立价格或效能服务当前不可用；已进入纯历史推荐模式。"],
            "product_code": self.product_code,
            "schema_keys": [],
            "feature_compatibility": {},
            "price": {"model_kind": "price", "backend": "unavailable", "model_version": None,
                      "product_code": None, "artifact_sha256": None},
            "effectiveness": {"model_kind": "effectiveness", "backend": "unavailable", "model_version": None,
                              "product_code": None, "artifact_sha256": None,
                              "target_protocol_contract": {}, "capabilities": {}},
            "execution_mode": "independent_http_services",
            "calculation_available": False,
            "historical_recommendation_available": True,
            "local_fallback_enabled": False,
        }

    def evaluate(self, params, target_protocol=None):
        raise ModelServiceUnavailable("独立价格或效能服务不可用；当前仅支持已有历史成品推荐。")
