# -*- coding: utf-8 -*-
from __future__ import print_function

import math


DEFAULT_FEASIBILITY_GATE = 0.65


def effective_price(item):
    base=float(item.get('predicted_price_wan',item.get('historical_price_wan',0)) or 0)
    conservative=item.get('recommendation_price_wan',(item.get('evaluation') or {}).get('recommendation_price_wan',base))
    try:
        conservative=float(conservative)
        return max(base,conservative) if math.isfinite(conservative) else base
    except (ValueError,TypeError):return base


def _number(value):
    try:
        if isinstance(value, str) and value.strip().upper().startswith("IP"):
            value = value.strip()[2:]
        return float(value)
    except (TypeError, ValueError):
        return None


def _interval(value):
    if isinstance(value, (list, tuple)) and len(value) >= 2:
        a, b = _number(value[0]), _number(value[1])
        return (min(a, b), max(a, b)) if a is not None and b is not None else None
    if isinstance(value, dict):
        a = _number(value.get("min", value.get("lower")))
        b = _number(value.get("max", value.get("upper")))
        return (min(a, b), max(a, b)) if a is not None and b is not None else None
    n = _number(value)
    return (n, n) if n is not None else None


def filter_match(params, rule):
    key = rule.get("parameter_id")
    operator = rule.get("operator", "eq")
    if not key or key not in params:
        return False
    actual = params.get(key)
    value1 = rule.get("value1")
    value2 = rule.get("value2")
    if operator == "boolean_is":
        truth = str(value1).strip().lower() in ("1", "true", "yes", "有", "是") or value1 is True or value1 == 1
        actual_truth = str(actual).strip().lower() in ("1", "true", "yes", "有", "是") or actual is True or actual == 1
        return actual_truth == truth
    if operator in ("text_equals", "text_contains"):
        left, right = str(actual).strip().lower(), str(value1 or "").strip().lower()
        return left == right if operator == "text_equals" else right in left
    a = _number(actual)
    b = _number(value1)
    if operator in ("gt", "gte", "lt", "lte", "eq"):
        if a is None or b is None:
            return False
        if operator == "gt": return a > b
        if operator == "gte": return a >= b
        if operator == "lt": return a < b
        if operator == "lte": return a <= b
        return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
    actual_range = _interval(actual)
    requested = _interval([value1, value2])
    if not actual_range or not requested:
        return False
    al, ah = actual_range; rl, rh = requested
    if operator == "range_inside": return al >= rl and ah <= rh
    if operator == "range_contains": return al <= rl and ah >= rh
    if operator == "range_overlap": return max(al, rl) <= min(ah, rh)
    return False


def conservative_capability(item):
    value = item.get("conservative_capability_score")
    if value is None:
        value = (item.get("evaluation") or {}).get("conservative_capability_score")
    if value is None:
        value = item.get("capability_score", 0)
    return float(value or 0)


def physical_gate_passes(item, request):
    evaluation = item.get("evaluation") or {}
    gate = item.get("physical_gate") or evaluation.get("physical_gate") or {}
    if gate.get("passed") is False:
        return False
    if item.get("hard_risk_reasons") or evaluation.get("hard_risk_reasons"):
        return False
    feasibility = float(item.get("feasibility_probability", evaluation.get("feasibility_probability", 0)) or 0)
    requested = request.get("min_feasibility")
    threshold = DEFAULT_FEASIBILITY_GATE if requested in (None, "") else max(DEFAULT_FEASIBILITY_GATE, float(requested))
    return feasibility >= threshold


def agreement_matches(item, request):
    if item.get("model_evaluation_available") is False:
        price = _number(item.get("historical_price_wan"))
        if request.get("max_price") not in (None, ""):
            if price is None or price > float(request["max_price"]):
                return False
        filters = request.get("indicator_filters") or []
        if filters:
            results = [filter_match(item.get("params", {}), rule) for rule in filters]
            if request.get("indicator_filter_mode", "all") == "any":
                if not any(results):
                    return False
            elif not all(results):
                return False
        # Model-only thresholds cannot be judged for this one legacy row. Keep
        # it visible with an explicit "service unavailable" badge instead.
        return True
    price = effective_price(item)
    capability = conservative_capability(item)
    ce = float(item.get("cost_effectiveness", capability / max(price, 1e-9)) or 0)
    feasibility = float(item.get("feasibility_probability", 0) or 0)
    if not physical_gate_passes(item, request): return False
    if request.get("max_price") not in (None, "") and price > float(request["max_price"]): return False
    if request.get("min_capability") not in (None, "") and capability < float(request["min_capability"]): return False
    if request.get("min_cost_effectiveness") not in (None, "") and ce < float(request["min_cost_effectiveness"]): return False
    if request.get("min_feasibility") not in (None, "") and feasibility < max(DEFAULT_FEASIBILITY_GATE, float(request["min_feasibility"])): return False
    filters = request.get("indicator_filters") or []
    if filters:
        results = [filter_match(item.get("params", {}), rule) for rule in filters]
        if request.get("indicator_filter_mode", "all") == "any":
            if not any(results): return False
        elif not all(results):
            return False
    return True


def compute_tag_match(item, selected_tags, tag_weights):
    selected = list(selected_tags or [])
    if not selected:
        return 100.0
    own = set(item.get("tags") or [])
    denominator = sum(float(tag_weights.get(tag, 1.0)) for tag in selected)
    numerator = sum(float(tag_weights.get(tag, 1.0)) for tag in selected if tag in own)
    return 100.0 * numerator / max(denominator, 1e-9)


def rank_agreements(items, request, tag_weights):
    allow_best_effort = bool(request.get("include_best_effort"))
    candidates = [
        dict(item) for item in items
        if agreement_matches(item, request) or (allow_best_effort and item.get("best_effort"))
    ]
    selected_tags = request.get("selected_tags") or []
    for item in candidates:
        model_available = item.get("model_evaluation_available") is not False
        price = float(item.get("predicted_price_wan", item.get("historical_price_wan", 0)) or 0)
        capability = conservative_capability(item)
        item["conservative_capability_score"] = capability
        item["predicted_price_wan"] = round(price, 3)
        item['recommendation_price_wan']=effective_price(item)
        item["cost_effectiveness"] = round(capability / max(item['recommendation_price_wan'], 1e-9), 3)
        own_tags = set(item.get("tags") or [])
        item["matched_tags"] = [tag for tag in selected_tags if tag in own_tags]
        item["missing_tags"] = [tag for tag in selected_tags if tag not in own_tags]
        item["tag_match_score"] = round(compute_tag_match(item, selected_tags, tag_weights), 3)
        if not model_available:
            item["predicted_price_wan"] = item.get("historical_price_wan")
            item["capability_score"] = None
            item["conservative_capability_score"] = None
            item["cost_effectiveness"] = None
            item["feasibility_probability"] = None
    if not candidates:
        return []
    prices = [effective_price(item) for item in candidates]
    ces = [float(item.get("cost_effectiveness") or 0) for item in candidates]
    pmin, pmax = min(prices), max(prices)
    cemin, cemax = min(ces), max(ces)
    for item in candidates:
        numeric_price = effective_price(item)
        numeric_ce = float(item.get("cost_effectiveness") or 0)
        price_score = 100.0 if pmax == pmin else 100.0 * (pmax - numeric_price) / (pmax - pmin)
        ce_score = 100.0 if cemax == cemin else 100.0 * (numeric_ce - cemin) / (cemax - cemin)
        feasibility_score = 100.0 * float(item.get("feasibility_probability", 0.75))
        uncertainty_width = float(item.get("score_uncertainty_width", (item.get("evaluation") or {}).get("score_uncertainty_width", 0)) or 0)
        uncertainty_penalty = min(10.0, 0.20 * uncertainty_width)
        item["price_score"] = round(price_score, 3)
        item["cost_effectiveness_score"] = round(ce_score, 3)
        item["uncertainty_penalty"] = round(uncertainty_penalty, 3)
        if item.get("model_evaluation_available") is False:
            base_score = 0.70 * item["tag_match_score"] + 0.30 * price_score - 20.0
        else:
            base_score = (
                0.30 * item["tag_match_score"] + 0.25 * float(item.get("conservative_capability_score", 0)) +
                0.15 * price_score + 0.15 * ce_score + 0.15 * feasibility_score
            ) - uncertainty_penalty
        if item.get("best_effort"):
            base_score -= min(35.0, 4.0 * float(item.get("fit_penalty", 0) or 0))
        item["comprehensive_score"] = round(base_score, 3)
    sort_by = request.get("sort_by", "comprehensive")
    key_map = {
        "comprehensive": "comprehensive_score",
        "price": "predicted_price_wan",
        "capability": "conservative_capability_score",
        "cost_effectiveness": "cost_effectiveness",
        "tag_match": "tag_match_score",
        "feasibility": "feasibility_probability",
    }
    key = key_map.get(sort_by, "comprehensive_score")
    reverse = sort_by != "price"
    candidates.sort(key=lambda item: effective_price(item) if sort_by=='price' else float(item.get(key) or 0), reverse=reverse)
    for index, item in enumerate(candidates, 1):
        item["rank"] = index
    return candidates


def rank_historical_products(items, request, tag_weights):
    """Rank stored history without inventing model outputs.

    Used when either independent HTTP model service is unavailable.  Model-only
    filters are intentionally ignored; historical price, business attributes
    and stored tags continue to work.
    """
    selected_tags = list(request.get("selected_tags") or [])
    filters = list(request.get("indicator_filters") or [])
    filter_mode = str(request.get("indicator_filter_mode") or "all")
    candidates = []
    for source in items:
        item = dict(source)
        price = _number(item.get("historical_price_wan"))
        if request.get("max_price") not in (None, ""):
            if price is None or price > float(request["max_price"]):
                continue
        results = [filter_match(item.get("params") or {}, rule) for rule in filters]
        if results and ((filter_mode == "any" and not any(results)) or
                        (filter_mode != "any" and not all(results))):
            continue
        own = set(item.get("tags") or [])
        item["matched_tags"] = [tag for tag in selected_tags if tag in own]
        item["missing_tags"] = [tag for tag in selected_tags if tag not in own]
        item["tag_match_score"] = round(compute_tag_match(item, selected_tags, tag_weights), 3)
        item["predicted_price_wan"] = price
        item["capability_score"] = None
        item["conservative_capability_score"] = None
        item["cost_effectiveness"] = None
        item["feasibility_probability"] = None
        item["model_evaluation_available"] = False
        item["recommendation_confidence"] = "unavailable"
        candidates.append(item)
    if not candidates:
        return []
    prices = [item["predicted_price_wan"] for item in candidates if item["predicted_price_wan"] is not None]
    pmin, pmax = (min(prices), max(prices)) if prices else (0.0, 0.0)
    for item in candidates:
        price = item["predicted_price_wan"]
        price_score = 50.0 if price is None else 100.0 if pmax == pmin else 100.0 * (pmax - price) / (pmax - pmin)
        item["price_score"] = round(price_score, 3)
        item["comprehensive_score"] = round(0.70 * item["tag_match_score"] + 0.30 * price_score, 3)
    sort_by = str(request.get("sort_by") or "comprehensive")
    if sort_by == "price":
        candidates.sort(key=lambda item: (item["predicted_price_wan"] is None,
                                          item["predicted_price_wan"] if item["predicted_price_wan"] is not None else float("inf")))
    elif sort_by == "tag_match":
        candidates.sort(key=lambda item: item["tag_match_score"], reverse=True)
    else:
        candidates.sort(key=lambda item: item["comprehensive_score"], reverse=True)
    for index, item in enumerate(candidates, 1):
        item["rank"] = index
    return candidates
