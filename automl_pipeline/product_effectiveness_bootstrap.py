# -*- coding: utf-8 -*-
"""Construct an explicitly provisional, explainable coupling/utility snapshot."""
from __future__ import print_function

import json
import math
import re
from pathlib import Path

import numpy as np

from app.data_master import normalize_preference
from app.model_runtime import EffectivenessBundle
from app.xlsx_utils import read_workbook_bytes
from app.product_table import clean_field_id
from tools.product_price_automl import write_json


def load_config(input_path, config_path=None, allow_legacy=True):
    if config_path:
        config = json.loads(Path(config_path).read_text(encoding="utf-8-sig"))
        if not isinstance(config, dict):
            raise ValueError("效能配置须为JSON对象。")
        return config
    config = {"attributes": {}, "couplings": []}
    if not allow_legacy:
        config["auto_discover_couplings"] = False
    if Path(input_path).suffix.lower() != ".xlsx":
        return config
    sheets = read_workbook_bytes(Path(input_path).read_bytes())
    attribute_sheet = sheets.get("效能属性配置", sheets.get("属性配置", []) if allow_legacy else [])
    for row in attribute_sheet[1:]:
        if len(row) >= 3 and str(row[0] or "").strip():
            if clean_field_id(row[0]) in config["attributes"]:
                raise ValueError("属性配置中的英文代号清理后重复：" + clean_field_id(row[0]))
            config["attributes"][clean_field_id(row[0])] = {"preference": str(row[1] or "中性"), "weight": float(row[2] or 0)}
    for row in sheets.get("耦合关系", [])[1:]:
        if row and str(row[0] or "").strip() in ("无", "无耦合", "无已知耦合", "none"):
            config["auto_discover_couplings"] = False
            continue
        if len(row) >= 2 and str(row[0] or "").strip():
            edge = {"source": clean_field_id(row[0]), "target": clean_field_id(row[1])}
            if len(row) > 2 and row[2] not in (None, ""):
                edge["coefficient"] = float(row[2])
                edge["offset"] = float(row[3] or 0) if len(row) > 3 else 0
                edge["tolerance"] = float(row[4]) if len(row) > 4 and row[4] not in (None, "") else None
            config["couplings"].append(edge)
    return config


def inferred_preference(label):
    if re.search("下限.*温度|温度下限|质量|重量|响应时间|功耗|误差|定位精度", label):
        return "lower"
    if re.search("是否|有无|载荷|推力|速度|寿命|防护|盐雾|温度上限|工作制|强度", label):
        return "higher"
    return "neutral"


def build_effectiveness(data, output_dir, config=None):
    config = config or {}
    if set(config) - {"attributes", "couplings", "hard_rules", "auto_discover_couplings"}:
        raise ValueError("效能配置仅支持attributes、couplings、hard_rules。")
    definitions = dict((p["parameter_id"], p) for p in data["parameters"])
    overrides = config.get("attributes") or {}
    unknown = set(overrides) - set(definitions)
    if unknown:
        raise ValueError("效能属性配置引用未知代号：%s" % ",".join(sorted(unknown)))
    numeric = [p for p in data["parameters"] if p["value_type"] in ("number", "boolean", "ip_grade") and p["inference"]["observed_count"]]
    if not numeric:
        raise ValueError("效能试用模型至少需要一个数值或布尔属性。")
    features, assumptions, weights, ranges = [], [], {}, {}
    for p in numeric:
        key = p["parameter_id"]
        values = np.asarray([row["params"][key] for row in data["agreements"] if key in row["params"]], dtype=float)
        if not np.all(np.isfinite(values)):
            raise ValueError("效能属性%s包含非有限值。" % p["label"])
        low, high = float(values.min()), float(values.max())
        if p["value_type"] == "boolean":
            low, high = 0.0, 1.0
        elif low == high:
            low, high = low - 0.5, high + 0.5
        supplied = overrides.get(key, {})
        direction = normalize_preference(supplied.get("preference", inferred_preference(p["label"])))
        if direction not in ("lower", "higher", "neutral"):
            raise ValueError("属性%s的偏好方向只能为越大越好、越小越好或中性。" % p["label"])
        weight = float(supplied.get("weight", 1 if direction != "neutral" else 0))
        if not math.isfinite(weight) or weight < 0:
            raise ValueError("效能权重须为非负有限数。")
        weights[key] = weight
        p["preference"] = direction
        feature = {"key": key, "label": p["label"], "unit": p.get("unit", ""),
                   "type": "boolean" if p["value_type"] == "boolean" else "number",
                   "min": low, "max": high, "preference": direction, "required": bool(p["required"]),
                   "missing_policy": "reject" if p["required"] else "default",
                   "default_value": float(np.unique(values, return_counts=True)[0][np.argmax(np.unique(values, return_counts=True)[1])]) if p["value_type"] == "boolean" else float(np.median(values)),
                   "auto_adjustable": True, "parser": "ip_grade" if p["value_type"] == "ip_grade" else None}
        features.append(feature)
        ranges[key] = [float(values.min()), float(values.max())]
        assumptions.append({"field_id": key, "label": p["label"], "preference": direction,
                            "weight": weight, "source": "user_configuration" if key in overrides else "label_heuristic_pending_confirmation"})
    feature_map = dict((x["key"], x) for x in features)
    edges = list(config.get("couplings") or [])
    discovery = not edges and config.get("auto_discover_couplings", True)
    if discovery:
        # Suggestions use earlier columns as sources only; table order is not causality.
        eligible = [p["parameter_id"] for p in numeric if p["value_type"] != "boolean"]
        for target_index, target in enumerate(eligible):
            candidates = []
            for source in eligible[:target_index]:
                pairs = np.asarray([[r["params"][source], r["params"][target]] for r in data["agreements"] if source in r["params"] and target in r["params"]], dtype=float)
                if len(pairs) < 12 or np.ptp(pairs[:, 0]) == 0 or np.ptp(pairs[:, 1]) == 0:
                    continue
                correlation = float(np.corrcoef(pairs.T)[0, 1])
                if abs(correlation) >= 0.65:
                    candidates.append((abs(correlation), source))
            if candidates:
                edges.append({"source": max(candidates)[1], "target": target})
            if len(edges) >= 4:
                break
    models, coupling_reports, seen_targets = [], [], set()
    for index, edge in enumerate(edges):
        source, target = edge.get("source"), edge.get("target")
        if source not in feature_map or target not in feature_map or source == target:
            raise ValueError("耦合源和目标须为不同的数值/布尔英文属性代号：%s/%s" % (source, target))
        if target in seen_targets:
            raise ValueError("试用快照的每个目标仅接受一条耦合；目标重复：%s" % target)
        seen_targets.add(target)
        src, tgt = feature_map[source], feature_map[target]
        source_span, target_span = src["max"] - src["min"], tgt["max"] - tgt["min"]
        pairs = np.asarray([[r["params"][source], r["params"][target]] for r in data["agreements"] if source in r["params"] and target in r["params"]], dtype=float)
        supplied_coefficient = "coefficient" in edge
        if supplied_coefficient:
            raw_coefficient, raw_offset = float(edge["coefficient"]), float(edge.get("offset", 0))
            coefficient = raw_coefficient * source_span / target_span
            intercept = (raw_coefficient * src["min"] + raw_offset - tgt["min"]) / target_span
        else:
            if len(pairs) < 8 or np.ptp(pairs[:, 0]) <= 1e-12:
                raise ValueError("耦合%s/%s至少需要8条完整记录且源属性有变化。" % (source, target))
            x = (pairs[:, 0] - src["min"]) / source_span
            y = (pairs[:, 1] - tgt["min"]) / target_span
            intercept, coefficient = [float(x) for x in np.linalg.lstsq(np.column_stack([np.ones(len(x)), x]), y, rcond=None)[0]]
            raw_coefficient = coefficient * target_span / source_span
            raw_offset = tgt["min"] + intercept * target_span - raw_coefficient * src["min"]
        if not all(math.isfinite(v) for v in (coefficient, intercept, raw_coefficient, raw_offset)):
            raise ValueError("耦合参数须为有限数。")
        if edge.get("tolerance") is not None:
            tolerance = float(edge["tolerance"])
            if not math.isfinite(tolerance) or tolerance <= 0:
                raise ValueError("耦合容差须为正有限数。")
            lower, upper = -tolerance / target_span, tolerance / target_span
        elif len(pairs):
            residuals = (pairs[:, 1] - (raw_coefficient * pairs[:, 0] + raw_offset)) / target_span
            lower, upper = min(float(np.quantile(residuals, .05)), -.05), max(float(np.quantile(residuals, .95)), .05)
        else:
            lower, upper = -.1, .1
        models.append({"target": target, "sources": [{"key": source, "coefficient": coefficient}],
                       "intercept": intercept, "lower_offset": lower, "upper_offset": upper})
        relation = "工程先验" if supplied_coefficient else "自动统计建议（待确认）" if discovery else "指定关系的样本拟合"
        coupling_reports.append({"source": source, "source_label": src["label"], "target": target, "target_label": tgt["label"],
                                 "raw_coefficient": raw_coefficient, "raw_offset": raw_offset, "sample_count": len(pairs),
                                 "source_kind": relation, "inference": "经验轮廓区间，不代表因果关系或已校准可行性"})
        data["couplings"].append({"coupling_id": "BOOT_C%03d" % (index + 1),
                                  "coupling_name": "%s与%s" % (src["label"], tgt["label"]),
                                  "coupling_type": "positive" if coefficient >= 0 else "negative",
                                  "parameter_a": source, "parameter_b": target, "domain_operator": None,
                                  "multiplier": raw_coefficient, "offset": raw_offset, "strength": .5,
                                  "severity": "info", "description": relation, "rationale": "试用模型经验轮廓，需工程确认",
                                  "display_order": index + 1, "enabled": 1})
    total_weight = sum(weights.values())
    product = data["products"][0]
    bundle = {"manifest": {"contract_version": "3.0", "model_version": "prototype-coupling-v1",
                            "product_code": product["product_code"], "training_source": "provisional_coupling_baseline",
                            "model_type": "coupling_contours_and_assumed_additive_utility", "prototype": True},
              "schema": {"product_code": product["product_code"], "product_name": product["product_name"],
                         "features": features, "generator_order": [p["key"] for p in features]},
              "coupling_models": models, "training_ranges": ranges, "hard_rules": config.get("hard_rules") or [],
              "capability_model": {"intercept": 20.0 if total_weight else 50.0,
                                   "weights": dict((k, 80 * v / total_weight) for k, v in weights.items()) if total_weight else {}},
              "feasibility_model": {"intercept": 3.0, "weights": {"coupling_violation": -2.0, "hard_violation_count": -8.0,
                                                                               "outside_training_range": -3.0}},
              "capability_source": "coupling_prototype_pending_expert_validation"}
    errors, warnings = EffectivenessBundle(bundle).validate()
    if errors:
        raise ValueError("效能试用模型校验失败：%s" % "；".join(errors))
    report = {"prototype": True, "expert_trained": False, "expert_configured": bool(overrides), "feasibility_calibrated": False,
              "preference_assumptions": assumptions, "coupling_models": coupling_reports,
              "excluded_fields": [p["parameter_id"] for p in data["parameters"] if p not in numeric],
              "warnings": warnings + ["效能权重与方向来自预设配置；尚未使用真实效能/可行性标注训练。" if overrides else "效能权重和可行性映射为试用假设，未使用专家偏好或真实可行性标注。",
                                       "经验范围取自样本，不能当作工程极限。"],
              "automatic_statistical_coupling_suggestions": discovery}
    output_dir = Path(output_dir)
    write_json(output_dir / "effectiveness_bundle.json", bundle)
    write_json(output_dir / "bootstrap_report.json", report)
    return bundle, report


def add_attribute_tags(data):
    for p in data["parameters"]:
        key, dtype = p["parameter_id"], p["value_type"]
        if dtype != "boolean":
            continue
        tag_id = "BOOT_TAG_%s" % key
        label = re.sub(r"^(是否|有无)", "", p["label"])
        data["tags"].append({"tag_id": tag_id, "tag_name": label, "tag_group": "功能", "weight": 1.0,
                             "derivation_mode": "rule", "description": "由功能属性自动建立", "enabled": 1})
        data["tag_rules"].append({"rule_id": "BOOT_RULE_%s" % key, "tag_id": tag_id, "parameter_id": key,
                                 "operator": "boolean_is", "value1": "1", "value2": None, "rule_group": 1, "enabled": 1})
        for row in data["agreements"]:
            if row["params"].get(key) == 1:
                row["tags"].append(tag_id)
