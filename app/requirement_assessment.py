# -*- coding: utf-8 -*-
"""Shared requirement assessment for historical recommendation and generation.

One source of truth for "does this candidate meet the user's conditions, and if
not, by how much".  It returns per-condition evidence plus a numeric penalty so
the same logic powers historical soft-recommendation, seed selection and the
generator's demand anchoring.
"""
from __future__ import print_function

from .conditional_constraint import TEMPLATE_KIND, parse_template_metadata
from .recommender import filter_match
from .value_semantics import is_special_value, normalize_numeric


def _number(value):
    return normalize_numeric(value)


def _rule_gap(params, rule, definition, matched):
    """Normalised distance to satisfying one indicator rule (0.0 when satisfied).

    Numeric operators use the attribute's own span so ``99 vs 100`` and ``10 vs
    100`` are no longer the same binary penalty; boolean/text/range rules that
    fail collapse to 1.0 (there is no meaningful continuous distance).
    """
    if matched:
        return 0.0
    operator = rule.get("operator")
    value1 = rule.get("value1")
    value2 = rule.get("value2")
    actual = params.get(rule.get("parameter_id"))
    definition = definition or {}
    if is_special_value(definition, actual):
        return 1.0
    span = max(float(definition.get("max_value") or 1) - float(definition.get("min_value") or 0), 1e-9)
    a = _number(actual)
    if operator in ("gte", "gt", "lte", "lt", "eq"):
        b = _number(value1)
        if a is None or b is None:
            return 1.0
        if operator == "gte":
            return (b - a) / span
        if operator == "gt":
            return (b - a + 1e-7) / span
        if operator == "lte":
            return (a - b) / span
        if operator == "lt":
            return (a - b + 1e-7) / span
        return abs(a - b) / span
    if str(operator).startswith("range_") and a is not None:
        b1, b2 = _number(value1), _number(value2)
        if b1 is not None and b2 is not None:
            lo, hi = min(b1, b2), max(b1, b2)
            return (lo - a if a < lo else a - hi) / span
    return 1.0


def _business_gap(params, rule, definition, matched):
    """Return the unmet distance in the parameter's original business unit.

    This value is presentation evidence.  It must never be divided by a
    DataMaster/Schema span.  Enum, boolean, text and special states have no
    meaningful numeric distance and therefore return ``None``.
    """
    if matched:
        return 0.0
    definition = definition or {}
    operator = rule.get("operator")
    actual = params.get(rule.get("parameter_id"))
    value_type = str(definition.get("value_type") or "").lower()
    search_type = str(definition.get("search_type") or "").lower()
    if (operator in ("boolean_is", "special_is", "text_equals", "text_contains")
            or value_type in ("boolean", "bool", "enum", "text", "string")
            or search_type in ("boolean", "unordered_enum")
            or is_special_value(definition, actual)):
        return None
    a = _number(actual)
    b1 = _number(rule.get("value1"))
    b2 = _number(rule.get("value2"))
    if a is None or b1 is None:
        return None
    if operator in ("lte", "lt"):
        return max(0.0, a - b1)
    if operator in ("gte", "gt"):
        return max(0.0, b1 - a)
    if operator == "eq":
        return abs(a - b1)
    if str(operator).startswith("range_") and b2 is not None:
        lo, hi = min(b1, b2), max(b1, b2)
        return max(0.0, lo - a) if a < lo else max(0.0, a - hi)
    return None


def assess_requirements(item, request, definitions=None, tag_map=None, constraint_rules=None):
    """Assess one candidate against the user's requirements.

    Returns::

        {
          "conditions": [ {kind, key, label, operator, target, actual, matched, gap}, ... ],
          "matched_count", "unmatched_count", "unknown_count", "total_count",
          "demand_penalty", "strict_satisfied", "fit_ratio",
        }

    ``unknown`` means the value cannot be judged right now (e.g. the model
    service is down); it is never treated as a failure.
    """
    definitions = definitions or {}
    tag_map = tag_map or {}
    conditions = []
    penalty = 0.0
    matched = 0
    unmatched = 0
    unknown = 0
    hard_unmatched = 0

    # Conditional-attribute targets: whose controller currently forces them
    # inactive, so evidence can say "当前无该属性" instead of a raw gap.
    conditional_targets = {}
    for rule in (constraint_rules or []):
        kind = rule.get("rule_kind")
        if kind in ("conditional_lower", "conditional_upper"):
            meta = parse_template_metadata(rule)
            if meta and meta.get("template") == TEMPLATE_KIND and meta.get("target"):
                conditional_targets.setdefault(meta["target"], meta)

    price = item.get("predicted_price_wan")
    if price in (None, ""):
        price = item.get("historical_price_wan")
    price_num = _number(price)
    max_price = request.get("max_price")
    if max_price not in (None, ""):
        target = float(max_price)
        actual = price_num
        if actual is None:
            status, gap = "unknown", None
            unknown += 1
        elif actual <= target:
            status, gap = "matched", 0.0
            matched += 1
        else:
            status, gap = "unmatched", round(actual - target, 6)
            unmatched += 1
            hard_unmatched += 1
            penalty += 1.5 + gap / max(abs(target), 1.0)
        conditions.append({
            "kind": "price", "key": "max_price", "label": "价格 ≤ %.3f万元" % target,
            "operator": "lte", "target": target, "actual": actual, "matched": status == "matched", "status": status,
            "gap": gap, "business_gap": gap, "normalized_gap": (round(gap / max(abs(target), 1.0), 6) if gap is not None else None), "unit": "万元",
        })

    capability = _number(item.get("capability_score"))
    min_capability = request.get("min_capability")
    if min_capability not in (None, ""):
        target = float(min_capability)
        if capability is None:
            status, gap = "unknown", None
            unknown += 1
        elif capability >= target:
            status, gap = "matched", 0.0
            matched += 1
        else:
            status, gap = "unmatched", round(target - capability, 6)
            unmatched += 1
            hard_unmatched += 1
            penalty += 1.5 + gap / max(abs(target), 1.0)
        conditions.append({
            "kind": "capability", "key": "min_capability", "label": "效能 ≥ %.3f" % target,
            "operator": "gte", "target": target, "actual": capability, "matched": status == "matched", "status": status,
            "gap": gap, "business_gap": gap, "normalized_gap": (round(gap / max(abs(target), 1.0), 6) if gap is not None else None), "unit": "分",
        })

    # Technical indicator filters: each explicit filter is one counted condition;
    # AND/OR semantics are reported separately in indicator_logic.
    rules = list(request.get("indicator_filters") or [])
    if rules:
        params = item.get("params") or {}
        results = [filter_match(params, rule, definitions.get(rule.get("parameter_id"))) for rule in rules]
        gaps = [_rule_gap(params, rule, definitions.get(rule.get("parameter_id")), ok) for rule, ok in zip(rules, results)]
        business_gaps = [_business_gap(params, rule, definitions.get(rule.get("parameter_id")), ok) for rule, ok in zip(rules, results)]
        # An inactive subordinate (-1 = 无该属性) is "not applicable", not a
        # continuous numeric distance: flatten its gap to a fixed 1.0.
        inactive_flags = {}
        for rule in rules:
            key = rule.get("parameter_id")
            meta = conditional_targets.get(key)
            if meta is not None:
                actual_num = _number(params.get(key))
                if actual_num is not None and abs(actual_num - float(meta.get("inactive_value", -1))) < 1e-9:
                    inactive_flags[key] = meta
        if inactive_flags:
            gaps = [1.0 if rule.get("parameter_id") in inactive_flags else gap for rule, gap in zip(rules, gaps)]
            business_gaps = [None if rule.get("parameter_id") in inactive_flags else gap for rule, gap in zip(rules, business_gaps)]
        mode = str(request.get("indicator_filter_mode") or "all")
        group_matched = any(results) if mode == "any" else all(results)
        if group_matched:
            group_gap = 0.0
        else:
            # The group's gap must express the same demand distance that feeds
            # demand_penalty: the closest alternative for ANY, the sum for AND.
            group_gap = min(gaps) if mode == "any" else sum(gaps)
            hard_unmatched += 1
            penalty += group_gap
        indicator_matched = 0
        indicator_unmatched = 0
        for rule, ok, gap, business_gap in zip(rules, results, gaps, business_gaps):
            key = rule.get("parameter_id")
            definition = definitions.get(key, {})
            label = definition.get("label", key)
            operator = rule.get("operator")
            value1 = rule.get("value1")
            value2 = rule.get("value2")
            if ok:
                matched += 1
                indicator_matched += 1
            else:
                unmatched += 1
                indicator_unmatched += 1
            condition = {
                "kind": "parameter", "key": key, "parameter_id": key,
                "label": label, "operator": operator,
                "target": value1, "target2": value2, "actual": params.get(key),
                "matched": bool(ok), "status": "matched" if ok else "unmatched",
                # ``gap`` remains an alias for the historic normalised ranking
                # distance.  User-facing explanations must use business_gap.
                "gap": round(gap, 6), "normalized_gap": round(gap, 6),
                "business_gap": (round(business_gap, 6) if business_gap is not None else None),
                "unit": definition.get("unit") or "", "group": mode, "group_matched": group_matched,
            }
            if is_special_value(definition, params.get(key)) and operator != "special_is":
                condition["actual_state"] = "special"
                condition["reason"] = "special_state_not_numeric"
            meta = inactive_flags.get(key)
            if meta is not None:
                condition["actual_state"] = "inactive"
                condition["inactive_reason"] = {
                    "controller": meta.get("controller"),
                    "controller_value": params.get(meta.get("controller")),
                }
            conditions.append(condition)
        indicator_logic = {
            "mode": mode,
            "satisfied": group_matched,
            "gap": round(group_gap, 6), "normalized_gap": round(group_gap, 6),
            "matched_count": indicator_matched,
            "unmatched_count": indicator_unmatched,
            "total_count": len(rules),
        }
    else:
        indicator_logic = None

    selected_tags = list(request.get("selected_tags") or [])
    own_tags = set(item.get("tags") or [])
    for tag_id in selected_tags:
        label = tag_map.get(tag_id, {}).get("tag_name", tag_id)
        ok = tag_id in own_tags
        if ok:
            matched += 1
        else:
            unmatched += 1
            hard_unmatched += 1
            penalty += 2.0 * float(tag_map.get(tag_id, {}).get("weight", 1.0))
        conditions.append({
            "kind": "tag", "key": tag_id, "label": label,
            "matched": ok, "status": "matched" if ok else "unmatched", "gap": None,
        })

    total = matched + unmatched + unknown
    strict_satisfied = hard_unmatched == 0 and unknown == 0
    if hard_unmatched > 0:
        assessment_status = "partial"
    elif unknown > 0:
        assessment_status = "unknown"
    else:
        assessment_status = "satisfied"
    fit_ratio = round(matched / total, 4) if total else 1.0
    return {
        "conditions": conditions,
        "matched_count": matched,
        "unmatched_count": unmatched,
        "unknown_count": unknown,
        "total_count": total,
        "indicator_logic": indicator_logic,
        "demand_penalty": round(penalty, 6),
        "strict_satisfied": strict_satisfied,
        "assessment_status": assessment_status,
        "fit_ratio": fit_ratio,
    }


def _operator_text(operator):
    return {
        "gte": "不低于", "gt": "高于", "lte": "不高于", "lt": "低于", "eq": "等于",
        "boolean_is": "为", "special_is": "状态为", "text_equals": "等于", "text_contains": "包含",
        "range_inside": "位于区间", "range_contains": "覆盖区间", "range_overlap": "与区间相交",
    }.get(operator, operator)
