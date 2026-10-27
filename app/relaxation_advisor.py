# -*- coding: utf-8 -*-
"""Current-pool, demand-bound minimal relaxation suggestions."""
from __future__ import print_function

from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from .requirement_assessment import assess_requirements


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _count_strict(candidates, request, definitions, tag_map, constraints):
    return sum(1 for item in candidates if assess_requirements(
        item, request, definitions, tag_map, constraints).get("strict_satisfied"))


def _step(definition):
    search_type = str((definition or {}).get("search_type") or "").lower()
    if search_type in ("integer", "ip_grade"):
        return 1.0
    try:
        places = max(0, min(int((definition or {}).get("decimal_places", 3)), 9))
    except (TypeError, ValueError):
        places = 3
    return 10.0 ** (-places)


def _inclusive_threshold(actual, operator, definition):
    """Smallest displayed threshold that admits actual under a strict operator."""
    places = max(0, int((definition or {}).get("decimal_places", 3) or 0))
    if str((definition or {}).get("search_type") or "").lower() in ("integer", "ip_grade"):
        places = 0
    step = Decimal(str(_step(definition)))
    value = Decimal(str(actual))
    quotient = value / step
    floor_grid = quotient.to_integral_value(rounding=ROUND_FLOOR) * step
    ceil_grid = quotient.to_integral_value(rounding=ROUND_CEILING) * step
    on_grid = value == floor_grid
    if operator == "lte":
        threshold = ceil_grid
    elif operator == "lt":
        threshold = floor_grid + step
    elif operator == "gte":
        threshold = floor_grid
    elif operator == "gt":
        threshold = ceil_grid - step if on_grid else floor_grid
    else:
        threshold = value
    return round(float(threshold), places)


def build_relaxation_suggestions(request, candidates, definitions, tag_map,
                                 constraints, requirement_version=None, limit=5):
    """Suggest only changes to explicit user demand, never engineering rules."""
    request = dict(request or {})
    candidates = list(candidates or [])
    version = requirement_version or {}
    suggestions = []

    def add(kind, label, before, after, unit, patch, condition_id):
        changed = dict(request)
        changed.update(patch)
        count = _count_strict(candidates, changed, definitions, tag_map, constraints)
        if count <= 0:
            return False
        delta = None
        if _num(before) is not None and _num(after) is not None:
            delta = abs(float(after) - float(before))
        suggestions.append({
            "condition_id": condition_id, "kind": kind, "label": label,
            "before": before, "after": after, "unit": unit or "",
            "business_delta": delta, "current_pool_new_strict_count": count,
            "scope_label": "当前方案库", "apply_patch": patch,
            "demand_version_id": version.get("id"),
            "demand_fingerprint": version.get("demand_fingerprint"),
        })
        return True

    max_price = _num(request.get("max_price"))
    if max_price is not None:
        values = sorted(set(_num(item.get("predicted_price_wan")) for item in candidates
                            if _num(item.get("predicted_price_wan")) is not None))
        for value in values:
            if value > max_price:
                if add("numeric_threshold", "最高价格", max_price, value, "万元",
                       {"max_price": value}, "max_price"):
                    break
    min_capability = _num(request.get("min_capability"))
    if min_capability is not None:
        values = sorted(set(_num(item.get("capability_score")) for item in candidates
                            if _num(item.get("capability_score")) is not None), reverse=True)
        for value in values:
            if value < min_capability:
                if add("numeric_threshold", "最低效能", min_capability, value, "分",
                       {"min_capability": value}, "min_capability"):
                    break

    filters = list(request.get("indicator_filters") or [])
    for index, rule in enumerate(filters):
        key = rule.get("parameter_id")
        definition = definitions.get(key) or {}
        operator = rule.get("operator")
        before = _num(rule.get("value1"))
        actuals = [_num((item.get("params") or {}).get(key)) for item in candidates]
        actuals = sorted(set(value for value in actuals if value is not None))
        if before is not None and operator in ("lte", "lt", "gte", "gt"):
            possible = [v for v in actuals if v > before] if operator in ("lte", "lt") else [v for v in reversed(actuals) if v < before]
            for value in possible:
                threshold = _inclusive_threshold(value, operator, definition)
                patched_filters = [dict(item) for item in filters]
                patched_filters[index]["value1"] = threshold
                if add("numeric_threshold", definition.get("label") or key, before, threshold,
                       definition.get("unit"), {"indicator_filters": patched_filters}, key):
                    break
        elif operator in ("between", "range_inside"):
            low, high = _num(rule.get("value1")), _num(rule.get("value2"))
            changes = []
            if low is not None and high is not None:
                for value in actuals:
                    if value < low:
                        changes.append((low-value, value, high))
                    elif value > high:
                        changes.append((value-high, low, value))
            for _delta, new_low, new_high in sorted(changes):
                patched_filters = [dict(item) for item in filters]
                patched_filters[index]["value1"] = new_low
                patched_filters[index]["value2"] = new_high
                if add("numeric_range", definition.get("label") or key,
                       "%s～%s" % (rule.get("value1"), rule.get("value2")),
                       "%s～%s" % (new_low, new_high), definition.get("unit"),
                       {"indicator_filters": patched_filters}, key):
                    break
        else:
            patched_filters = [dict(item) for pos, item in enumerate(filters) if pos != index]
            add("condition_removal", definition.get("label") or key, rule.get("value1"), None,
                definition.get("unit"), {"indicator_filters": patched_filters}, key)
    selected_tags = list(request.get("selected_tags") or [])
    for tag_id in selected_tags:
        patched_tags = [value for value in selected_tags if value != tag_id]
        label = (tag_map.get(tag_id) or {}).get("tag_name") or tag_id
        add("tag_removal", "取消功能偏好：%s" % label, label, None, "",
            {"selected_tags": patched_tags}, tag_id)
    suggestions.sort(key=lambda item: (item.get("business_delta") is None,
                                       item.get("business_delta") or 0,
                                       -item.get("current_pool_new_strict_count", 0)))
    return suggestions[:limit]
