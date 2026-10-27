# -*- coding: utf-8 -*-
"""Native price model bundle using standard-library pickle.

The bundle keeps the exact fitted estimator objects, scaler, feature order,
ensemble weights, target transformation and calibration information. joblib is
not required. Loading exact estimators still requires the same estimator
libraries and compatible versions used for training.
"""
from __future__ import print_function

import hashlib
import importlib
import json
import math
import os
import pickle
import platform
import sys
import time
from pathlib import Path


FORMAT_VERSION = "price-native-bundle-1.0"


def _version(module_name):
    try:
        module = importlib.import_module(module_name)
        return str(getattr(module, "__version__", "unknown"))
    except Exception:
        return None


def environment_versions():
    return {
        "python": "%s.%s.%s" % sys.version_info[:3],
        "platform": platform.platform(),
        "numpy": _version("numpy"),
        "scikit_learn": _version("sklearn"),
        "xgboost": _version("xgboost"),
        "joblib": _version("joblib"),
    }


def save_bundle(path, bundle):
    data = dict(bundle)
    data["format_version"] = FORMAT_VERSION
    data.setdefault("training_environment", environment_versions())
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    with temporary.open("wb") as handle:
        pickle.dump(data, handle, protocol=4)  # protocol 4 is supported by Python 3.8
    for attempt in range(9):
        try:
            os.replace(str(temporary),str(target));break
        except PermissionError:
            if attempt==8:raise
            time.sleep(min(.05*2**attempt,.5))
    return target


def load_bundle(path):
    with Path(path).open("rb") as handle:
        bundle = pickle.load(handle)
    if not isinstance(bundle, dict) or bundle.get("format_version") != FORMAT_VERSION:
        raise ValueError("价格模型文件不是%s格式" % FORMAT_VERSION)
    validate_bundle(bundle)
    return bundle


def active_model_names(bundle):
    return [str(item.get("name")) for item in (bundle.get("ensemble", {}).get("members") or [])]


def _category_mapping(spec):
    raw = spec.get("category_mapping")
    if isinstance(raw, dict) and raw:
        mapping = dict((str(key), float(value)) for key, value in raw.items())
    else:
        allowed = spec.get("allowed_values") or []
        mapping = dict((str(value), float(index)) for index, value in enumerate(allowed))
    if not mapping:
        raise ValueError("枚举字段%s缺少category_mapping或allowed_values" % (
            spec.get("field_name") or spec.get("key") or "（未知）"
        ))
    if any(not math.isfinite(value) for value in mapping.values()):
        raise ValueError("枚举字段映射值必须是有限数")
    return mapping


def validate_bundle(bundle):
    required = ["product_code", "model_version", "feature_order", "models", "ensemble", "target_transform"]
    missing = [name for name in required if name not in bundle]
    if missing:
        raise ValueError("价格模型包缺少: %s" % ",".join(missing))
    if not bundle.get("feature_order"):
        raise ValueError("feature_order为空")
    if not bundle.get("models"):
        raise ValueError("models为空")
    members = bundle.get("ensemble", {}).get("members") or []
    names = [str(item.get("name")) for item in members]
    if not names:
        raise ValueError("ensemble.members为空")
    if len(set(names)) != len(names):
        raise ValueError("ensemble.members包含重复模型名")
    unknown = [name for name in names if name not in bundle["models"]]
    if unknown:
        raise ValueError("集成配置引用不存在模型: %s" % ",".join(map(str, unknown)))
    weights = [float(item.get("weight", 0)) for item in members]
    if any((not math.isfinite(weight)) or weight < 0 for weight in weights):
        raise ValueError("集成权重必须是非负有限数")
    if not weights or sum(weights) <= 0:
        raise ValueError("集成权重无效")
    feature_order = [str(item) for item in bundle.get("feature_order") or []]
    schema = _field_map(bundle)
    missing_schema = [key for key in feature_order if key not in schema]
    if missing_schema:
        raise ValueError("feature_schema缺少字段: %s" % ",".join(missing_schema))
    for key in feature_order:
        spec = schema[key]
        dtype = str(spec.get("dtype") or spec.get("data_type") or "number").lower()
        if dtype in ("enum", "category", "categorical"):
            _category_mapping(spec)
    return True


def _parse_bool(value):
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return 1.0 if float(value) != 0 else 0.0
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "y", "on", "是", "有", "具备", "启用"):
        return 1.0
    if text in ("0", "false", "no", "n", "off", "否", "无", "不具备", "停用", ""):
        return 0.0
    raise ValueError("无法识别布尔值: %s" % value)


def _field_map(bundle):
    return dict((str(x.get("field_name") or x.get("key")), x) for x in bundle.get("feature_schema", []))


def prepare_vector(bundle, parameters):
    parameters = dict(parameters or {})
    specs = _field_map(bundle)
    values, filled, ignored, warnings = [], {}, [], []
    for key in bundle["feature_order"]:
        spec = specs.get(key, {})
        value = parameters.get(key)
        if value in (None, ""):
            policy = str(spec.get("missing_policy") or ("reject" if spec.get("required", True) else "training_mean"))
            if policy == "reject":
                raise ValueError("价格模型缺少必填字段%s" % key)
            if policy == "model_imputer":
                values.append(float("nan"))
                filled[key] = {"value": None, "strategy": "model_imputer"}
                continue
            if policy == "training_mean":
                value = spec.get("training_mean")
            elif policy in ("default", "configured_value", "constant"):
                value = spec.get("default_value", spec.get("configured_value", spec.get("constant")))
            elif policy == "zero":
                value = 0
            elif policy == "mode":
                value = spec.get("training_mode")
            else:
                raise ValueError("字段%s缺失策略不支持: %s" % (key, policy))
            if value in (None, ""):
                raise ValueError("字段%s缺失且补全值为空" % key)
            filled[key] = {"value": value, "strategy": policy}
        dtype = str(spec.get("dtype") or spec.get("data_type") or "number")
        if dtype == "boolean":
            numeric = _parse_bool(value)
        elif dtype == "ip_grade":
            numeric = float(str(value).strip().upper().replace("IP", ""))
        elif dtype in ("enum", "category", "categorical"):
            mapping = _category_mapping(spec)
            text = str(value).strip()
            if text not in mapping and isinstance(value,(int,float)) and not isinstance(value,bool):
                text=format(value,'.15g')
            if text in mapping:
                numeric = mapping[text]
            else:
                normalized = dict((key.strip().lower(), numeric_value) for key, numeric_value in mapping.items())
                lookup = text.lower()
                if lookup not in normalized:
                    raise ValueError(
                        "字段%s枚举值%s不在允许范围%s"
                        % (key, value, "、".join(mapping.keys()))
                    )
                numeric = normalized[lookup]
        else:
            numeric = float(value)
        if not math.isfinite(numeric):
            raise ValueError("价格字段%s须为有限数值" % key)
        lo, hi = spec.get("training_min"), spec.get("training_max")
        if lo is not None and numeric < float(lo):
            warnings.append("%s低于训练范围下限%s" % (spec.get("field_label", key), lo))
        if hi is not None and numeric > float(hi):
            warnings.append("%s高于训练范围上限%s" % (spec.get("field_label", key), hi))
        values.append(numeric)
    ignored = sorted(set(parameters) - set(bundle["feature_order"]))
    try:
        import numpy as np
        array = np.asarray([values], dtype=float)
    except Exception:
        array = [values]
    preprocessor = bundle.get("preprocessor")
    if preprocessor is not None:
        array = preprocessor.transform(array)
    return array, {"filled_fields": filled, "ignored_fields": ignored, "warnings": warnings}


def _model_prediction(model, prepared):
    value = model.predict(prepared)
    try:
        return float(value[0])
    except Exception:
        return float(value)


def price_risk(bundle,prepared,predictions,price_wan):
    region=bundle.get('price_trust_region')
    if region is None:return {}
    import numpy as np
    policy=bundle.get('price_risk_policy') or {};weights=np.asarray([max(0.,p['weight']) for p in predictions]);weights/=weights.sum()
    divisor=float(bundle.get('target_divisor_to_wan') or 1.)
    values=np.asarray([p['price_native']/divisor for p in predictions]);spread=float(np.sqrt(weights.dot((values-price_wan)**2)))
    risk=region.assess(prepared)[0]
    uplift=policy.get('disagreement_multiplier',1.)*spread+price_wan*policy.get('extrapolation_penalty',.1)*risk['severity']
    conservative=price_wan+max(0.,uplift)
    reference=risk.get('nearest_reference_price_native')
    if risk['is_extrapolation'] and reference is not None:conservative=max(conservative,float(reference)/divisor*min(1.,risk['severity']))
    risk.update(model_disagreement_wan=spread,recommendation_price_wan=round(conservative,6),note='保守排序价格；模型分歧和历史距离不是已校准置信区间')
    return risk


def predict(bundle, parameters, allow_degraded=False):
    prepared, input_status = prepare_vector(bundle, parameters)
    members = bundle["ensemble"]["members"]
    predictions, skipped = [], []
    for item in members:
        name = item["name"]
        model = bundle["models"].get(name)
        if model is None:
            skipped.append({"name": name, "reason": "model_missing"})
            continue
        try:
            raw = _model_prediction(model, prepared)
            if not math.isfinite(raw):
                raise ValueError("价格子模型输出不是有限数")
        except Exception as exc:
            if not allow_degraded:
                raise
            skipped.append({"name": name, "reason": str(exc)})
            continue
        if bundle.get("model_output_transform", "log") == "log":
            price_native = math.exp(raw)
        else:
            price_native = raw
        predictions.append({"name": name, "weight": float(item.get("weight", 1)), "raw": raw, "price_native": price_native})
    if not predictions:
        raise RuntimeError("没有可用的价格子模型")
    total_weight = sum(max(0.0, x["weight"]) for x in predictions)
    if total_weight <= 0:
        raise RuntimeError("可用模型权重总和为0")
    method = bundle.get("ensemble", {}).get("aggregation", "weighted_mean_price")
    if method == "weighted_mean_log_prediction":
        log_value = sum(max(0.0, x["weight"]) * x["raw"] for x in predictions) / total_weight
        price_native = math.exp(log_value)
    else:
        price_native = sum(max(0.0, x["weight"]) * x["price_native"] for x in predictions) / total_weight
    divisor = float(bundle.get("target_divisor_to_wan") or 1.0)
    price_wan = price_native / divisor
    residual = bundle.get("residual_calibration") or {}
    if residual.get("space") == "log":
        lower = price_wan * math.exp(float(residual.get("lower", 0)))
        upper = price_wan * math.exp(float(residual.get("upper", 0)))
    else:
        delta = float(residual.get("half_width_native", residual.get("half_width", 0))) / divisor
        lower, upper = max(0.0, price_wan - delta), price_wan + delta
    status = "exact" if not skipped else "degraded"
    result = {
        "predicted_price_wan": round(price_wan, 6),
        "price_interval_wan": [round(lower, 6), round(upper, 6)],
        "prediction_mode": "native_pickle_%s" % status,
        "member_predictions": predictions,
        "skipped_members": skipped,
        "input_status": input_status,
        "confidence": "low" if skipped or input_status["warnings"] or (bundle.get("automl_explanation") or {}).get("evidence_sufficient") is False else "medium",
    }
    risk=price_risk(bundle,prepared,predictions,price_wan)
    if risk:result.update(price_risk=risk,recommendation_price_wan=risk['recommendation_price_wan'])
    return result


def predict_batch(bundle, parameter_items, allow_degraded=False):
    """Predict a candidate matrix with one estimator call per ensemble member.

    Candidate generation commonly evaluates tens or hundreds of parameter sets.
    Calling every sklearn estimator once per row adds substantial Python overhead,
    especially for tree ensembles.  Preparation remains row-aware so missing-value
    and domain diagnostics are preserved, while fitted estimators receive the
    complete matrix in one call.
    """
    parameter_items = list(parameter_items or [])
    if not parameter_items:
        return []
    prepared_rows, input_statuses = [], []
    for parameters in parameter_items:
        prepared, input_status = prepare_vector(bundle, parameters)
        prepared_rows.append(prepared)
        input_statuses.append(input_status)
    try:
        from scipy import sparse
        if any(sparse.issparse(row) for row in prepared_rows):
            prepared_matrix = sparse.vstack(prepared_rows)
        else:
            import numpy as np
            prepared_matrix = np.vstack(prepared_rows)
    except Exception:
        prepared_matrix = []
        for row in prepared_rows:
            try:
                prepared_matrix.append(list(row[0]))
            except Exception:
                prepared_matrix.append(list(row))

    member_values, skipped = [], []
    for item in bundle["ensemble"]["members"]:
        name = item["name"]
        model = bundle["models"].get(name)
        if model is None:
            skipped.append({"name": name, "reason": "model_missing"})
            continue
        try:
            raw_values = model.predict(prepared_matrix)
            raw_values = [float(value) for value in raw_values]
            if len(raw_values) != len(parameter_items):
                raise ValueError("价格子模型批量返回数量不一致")
            if any(not math.isfinite(value) for value in raw_values):
                raise ValueError("价格子模型批量输出包含非有限数")
        except Exception as exc:
            if not allow_degraded:
                raise
            skipped.append({"name": name, "reason": str(exc)})
            continue
        member_values.append({
            "name": name,
            "weight": float(item.get("weight", 1)),
            "raw_values": raw_values,
        })
    if not member_values:
        raise RuntimeError("没有可用的价格子模型")
    total_weight = sum(max(0.0, item["weight"]) for item in member_values)
    if total_weight <= 0:
        raise RuntimeError("可用模型权重总和为0")

    method = bundle.get("ensemble", {}).get("aggregation", "weighted_mean_price")
    output_transform = bundle.get("model_output_transform", "log")
    divisor = float(bundle.get("target_divisor_to_wan") or 1.0)
    residual = bundle.get("residual_calibration") or {}
    results = []
    for row_index, input_status in enumerate(input_statuses):
        predictions = []
        for member in member_values:
            raw = member["raw_values"][row_index]
            price_native = math.exp(raw) if output_transform == "log" else raw
            predictions.append({
                "name": member["name"], "weight": member["weight"],
                "raw": raw, "price_native": price_native,
            })
        if method == "weighted_mean_log_prediction":
            log_value = sum(max(0.0, item["weight"]) * item["raw"] for item in predictions) / total_weight
            price_native = math.exp(log_value)
        else:
            price_native = sum(max(0.0, item["weight"]) * item["price_native"] for item in predictions) / total_weight
        price_wan = price_native / divisor
        if residual.get("space") == "log":
            lower = price_wan * math.exp(float(residual.get("lower", 0)))
            upper = price_wan * math.exp(float(residual.get("upper", 0)))
        else:
            delta = float(residual.get("half_width_native", residual.get("half_width", 0))) / divisor
            lower, upper = max(0.0, price_wan - delta), price_wan + delta
        status = "exact" if not skipped else "degraded"
        results.append({
            "predicted_price_wan": round(price_wan, 6),
            "price_interval_wan": [round(lower, 6), round(upper, 6)],
            "prediction_mode": "native_pickle_%s" % status,
            "member_predictions": predictions,
            "skipped_members": list(skipped),
            "input_status": input_status,
            "confidence": "low" if skipped or input_status["warnings"] or (bundle.get("automl_explanation") or {}).get("evidence_sufficient") is False else "medium",
        })
        risk=price_risk(bundle,prepared_rows[row_index],predictions,price_wan)
        if risk:results[-1].update(price_risk=risk,recommendation_price_wan=risk['recommendation_price_wan'])
    return results


def file_sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()
