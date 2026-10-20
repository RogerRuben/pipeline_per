# -*- coding: utf-8 -*-
"""Resolve price attribute references without confusing input IDs with feature use."""
import copy
import difflib
from pathlib import Path
from app.product_table import clean_field_id, TableFormatError

DEMO_SCORES = {"rated_thrust_n": 5, "peak_thrust_n": 3, "speed_mm_s": 5,
               "salt_spray_h": 4, "protection_grade": 4, "emergency_unlock": 4,
               "self_test": 4, "temp_high_c": 4, "noise_db": 2,
               "comm_latency_ms": 2, "sample_rate_hz": 1}
DEMO_CONFIGS = {"price_tabpfn_micro_robust.config.json", "price_tabpfn_attribute_scores.config.json"}


def migrate_bundled_example(path, supplied):
    """Recognize untouched shipped demo ratings; never rewrite a user's config."""
    if path is None:
        return supplied
    source = Path(path).resolve()
    examples = Path(__file__).resolve().parents[1] / "docs" / "examples"
    expert = supplied.get("expert") or {}
    if (source.parent == examples and source.name in DEMO_CONFIGS
            and expert.get("scores") == DEMO_SCORES and not expert.get("example_scores")):
        result = copy.deepcopy(supplied)
        result["expert"]["example_scores"] = result["expert"].pop("scores")
        return result
    return supplied


def field_index(known):
    index = {}
    for raw in known:
        token = clean_field_id(raw).casefold()
        if token in index and index[token] != raw:
            raise TableFormatError("成品属性英文代号清理后重复：%s/%s" % (index[token], raw),
                                   {"stage": "price_attribute_references", "field_id": raw})
        index[token] = raw
    return index


def resolve_field_mapping(mapping, known, source, allow_unknown=False):
    index = field_index(known)
    resolved, changes, ignored = {}, [], []
    for raw, value in (mapping or {}).items():
        cleaned = clean_field_id(raw)
        key = index.get(cleaned.casefold())
        if key is None:
            if allow_unknown:
                ignored.append({"field_id": str(raw), "source": source, "reason": "示例预设属性不在本次成品表内"})
                continue
            nearby = difflib.get_close_matches(cleaned.casefold(), sorted(index), n=3, cutoff=.55)
            suggestions = [index[item] for item in nearby]
            message = "价格属性评分设置（%s）引用的英文代号%r不在成品数据表第二行中。" % (source, raw)
            if suggestions:
                message += "相近代号：%s。" % "、".join(suggestions)
            raise TableFormatError(message, {"stage": "price_attribute_references", "source": source,
                                   "field_id": str(raw), "known_fields": sorted(known), "suggestions": suggestions})
        if key in resolved:
            raise TableFormatError("%s中多个代号清理后指向同一价格属性：%s。" % (source, key),
                                   {"stage": "price_attribute_references", "source": source, "field_id": key})
        resolved[key] = value
        if str(raw) != key:
            changes.append({"source": source, "raw_field_id": str(raw), "field_id": key})
    return resolved, changes, ignored


def resolve_price_expert(expert, parameters, workbook_scores=None, workbook_records=None,
                         workbook_types=None, workbook_groups=None):
    known = [parameter["parameter_id"] for parameter in parameters]
    result = copy.deepcopy(expert)
    examples, changes, ignored = resolve_field_mapping(result.get("example_scores"), known, "随包示例预设", True)
    explicit, normalized, _ = resolve_field_mapping(result.get("scores"), known, "JSON expert.scores")
    changes.extend(normalized)
    result["scores"] = dict(examples, **explicit)
    sources = dict((key, "example") for key in examples)
    sources.update((key, "configuration") for key in explicit)
    if workbook_scores:
        values, normalized, _ = resolve_field_mapping(workbook_scores, known, "工作簿价格属性评分")
        result["scores"].update(values)
        sources.update((key, "workbook") for key in values)
        changes.extend(normalized)
        result["records"] = copy.deepcopy(workbook_records or [])
    for name, supplied in (("field_types", workbook_types), ("attribute_groups", workbook_groups)):
        values, normalized, _ = resolve_field_mapping(result.get(name), known, "JSON expert." + name)
        changes.extend(normalized)
        if supplied:
            overrides, normalized, _ = resolve_field_mapping(supplied, known, "工作簿价格属性评分 " + name)
            values.update(overrides)
            changes.extend(normalized)
        result[name] = values
    return result, {"normalized_references": changes, "ignored_example_scores": ignored, "score_sources": sources}


def price_attribute_audit(parameters, usable, expert, resolution):
    usable = set(usable)
    rows = []
    for parameter in parameters:
        key = parameter["parameter_id"]
        observed = parameter.get("inference", {}).get("observed_count", 0)
        reason = ""
        if key not in usable:
            reason = ("整列为空或缺失标识，没有有效观测值" if not observed else
                      "文本属性未用于价格回归" if parameter["value_type"] == "text" else
                      "有观测值但带价格的训练记录中无有效值")
        score = float(expert["scores"].get(key, 3.))
        rows.append({"field_id": key, "label": parameter["label"], "score": score,
                     "specified": key in expert["scores"], "score_source": resolution["score_sources"].get(key, "neutral"),
                     "used_in_training": key in usable, "exclusion_reason": reason,
                     "value_type": parameter["value_type"], "observed_count": observed,
                     "multiplier_full_strength": float(2. ** ((score - 3.) / 2.))})
    return rows
