# -*- coding: utf-8 -*-
from __future__ import print_function

import math

from .value_semantics import canonical_filter_value, definition_mapping, is_special_value, mapping_target, normalize_boolean, normalize_numeric, values_equal


DEFAULT_FEASIBILITY_GATE = 0.65


def _number(value):
    return normalize_numeric(value)


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


def filter_match(params, rule, definition=None):
    key = rule.get("parameter_id")
    operator = rule.get("operator", "eq")
    if not key or key not in params:
        return False
    actual = params.get(key)
    value1 = rule.get("value1")
    value2 = rule.get("value2")
    if operator == "special_is":
        return is_special_value(definition, value1) and values_equal(actual, value1, definition)
    if operator == "boolean_is":
        # A mapped third state (无该属性 -> -1) must match the stored inactive
        # value; compare through the mapping before the two-value boolean truth.
        if values_equal(actual, value1, definition):
            return True
        target = mapping_target(value1, definition)
        if target is not None:
            a = normalize_numeric(actual)
            t = normalize_numeric(target)
            if a is not None and t is not None:
                return math.isclose(a, t, rel_tol=1e-9, abs_tol=1e-9)
            return str(actual).strip() == str(target).strip()
        truth = normalize_boolean(value1)
        actual_truth = normalize_boolean(actual)
        if truth is None or actual_truth is None:
            return values_equal(actual, value1, definition)
        return actual_truth == truth
    if operator in ("text_equals", "text_contains"):
        # Mapped enums compare through the DataMaster mapping so "常规型" matches a
        # stored model encoding such as 1 / 1.0 / "1".  Plain text keeps the
        # original case-insensitive trimmed comparison.
        if operator == "text_equals" and definition_mapping(definition):
            return values_equal(canonical_filter_value(actual, definition),
                                canonical_filter_value(value1, definition), definition)
        left, right = str(actual).strip().lower(), str(value1 or "").strip().lower()
        return left == right if operator == "text_equals" else right in left
    # A declared special state is a legal business value, but it is not a point
    # in the normal numeric domain.  Legacy exact equality remains compatible.
    if is_special_value(definition, actual):
        return operator == "eq" and is_special_value(definition, value1) and values_equal(actual, value1, definition)
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


def center_capability(item):
    """The user-facing effectiveness score: the model's center estimate.

    ``conservative_capability_score`` (P10) stays an internal risk signal; the
    operator-facing "效能评分" is always ``capability_score``.
    """
    value = item.get("capability_score")
    if value is None:
        value = (item.get("evaluation") or {}).get("capability_score")
    return float(value or 0)


def _price_value(item):
    """Return the item's usable price as a float, or None when it has no price.

    A historical sample without ``historical_price_wan`` (and therefore without
    a re-predicted price) must not be coerced to 0: that would make its
    cost-effectiveness explode and rank it first.
    """
    value = item.get("predicted_price_wan")
    if value in (None, ""):
        value = item.get("historical_price_wan")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def effective_price(item):
    """Stable public helper used by previously exported risk-aware models."""
    value=_price_value(item)
    if value is None:return 0.0
    conservative=item.get('recommendation_price_wan',(item.get('evaluation') or {}).get('recommendation_price_wan',value))
    try:
        conservative=float(conservative)
        return max(value,conservative) if math.isfinite(conservative) else value
    except (ValueError,TypeError):return value


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


def agreement_matches(item, request, definitions=None):
    definitions = definitions or {}
    if item.get("model_evaluation_available") is False:
        price = _number(item.get("historical_price_wan"))
        if request.get("max_price") not in (None, ""):
            if price is None or price > float(request["max_price"]):
                return False
        filters = request.get("indicator_filters") or []
        if filters:
            results = [filter_match(item.get("params", {}), rule, definitions.get(rule.get("parameter_id"))) for rule in filters]
            if request.get("indicator_filter_mode", "all") == "any":
                if not any(results):
                    return False
            elif not all(results):
                return False
        # Model-only thresholds cannot be judged for this one legacy row. Keep
        # it visible with an explicit "service unavailable" badge instead.
        return True
    price = effective_price(item) if _price_value(item) is not None else None
    capability = center_capability(item)
    ce = item.get("cost_effectiveness")
    if ce is None and price is not None:
        ce = capability / max(price, 1e-9)
    feasibility = float(item.get("feasibility_probability", 0) or 0)

    is_historical = (
        not item.get("is_generated")
        and item.get("agreement_source") in ("historical", "imported")
    )

    # Newly generated schemes must pass the model's engineering gate.  A real
    # historical product is never dropped by the model's opinion — only by the
    # user's own explicit filters; model risk stays visible as a warning.
    if not is_historical:
        if not physical_gate_passes(item, request):
            return False

    if request.get("max_price") not in (None, "") and (price is None or price > float(request["max_price"])): return False
    if request.get("min_capability") not in (None, "") and capability < float(request["min_capability"]): return False
    if request.get("min_cost_effectiveness") not in (None, "") and (ce is None or ce < float(request["min_cost_effectiveness"])): return False
    if request.get("min_feasibility") not in (None, "") and feasibility < float(request["min_feasibility"]): return False
    filters = request.get("indicator_filters") or []
    if filters:
        results = [filter_match(item.get("params", {}), rule, definitions.get(rule.get("parameter_id"))) for rule in filters]
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


def ranking_trace(request):
    """Describe the comparator actually used by both ranking paths.

    This trace is a response contract.  Explanations must consume it rather
    than infer a comparator from the optimization scenario.
    """
    request = request or {}
    sort_by = str(request.get("sort_by") or "comprehensive")
    key_map = {
        "comprehensive": "comprehensive_score", "price": "predicted_price_wan",
        "capability": "capability_score", "cost_effectiveness": "cost_effectiveness",
        "tag_match": "tag_match_score", "feasibility": "feasibility_probability",
    }
    display_map = {
        "comprehensive": "综合推荐", "price": "价格", "capability": "效能",
        "cost_effectiveness": "效费比", "tag_match": "功能偏好匹配",
        "feasibility": "可行性",
    }
    sort_order = str(request.get("sort_order") or "").strip().lower()
    if sort_order not in ("asc", "desc"):
        sort_order = "asc" if sort_by == "price" else "desc"
    source = str(request.get("sort_source") or
                 ((request.get("_scenario_policy") or {}).get("applied_ranking") or {}).get("source") or
                 "system_default")
    return {
        "stages": ["strict_satisfaction", "demand_penalty", "selected_sort"],
        "sort_key": sort_by,
        "value_field": key_map.get(sort_by, "comprehensive_score"),
        "sort_direction": sort_order,
        "sort_display_name": display_map.get(sort_by, "综合推荐"),
        "sort_source": source,
        "sort_source_display_name": "用户调整" if source == "user_override" else "场景默认",
    }


def rank_agreements(items, request, tag_weights, definitions=None, tag_map=None, constraint_rules=None):
    from .requirement_assessment import assess_requirements
    allow_best_effort = bool(request.get("include_best_effort"))
    candidates = []
    for source in items:
        item = dict(source)
        is_historical = (not item.get("is_generated")) and item.get("agreement_source") in ("historical", "imported")
        if is_historical:
            # Soft recommendation: a real historical product is never dropped by
            # the user's thresholds; it is kept and its requirement assessment is
            # attached so the UI can show exactly where it falls short.
            item["requirement_assessment"] = assess_requirements(item, request, definitions, tag_map, constraint_rules)
            item["strict_filter_satisfied"] = item["requirement_assessment"]["strict_satisfied"]
            item["fit_penalty"] = item["requirement_assessment"]["demand_penalty"]
            candidates.append(item)
        elif agreement_matches(item, request, definitions) or (allow_best_effort and item.get("best_effort")):
            assessment = assess_requirements(item, request, definitions, tag_map, constraint_rules)
            hard_penalty = float((item.get("search_metrics") or {}).get("hard_penalty") or 0)
            hard_conflicts = item.get("engineering_conflicts") or []
            item["requirement_assessment"] = assessment
            # A generated candidate's engineering hard conflicts are part of its
            # strictness and fit, not just a display flag (best_effort stays a
            # result-classification field, never a computation input).
            item["strict_filter_satisfied"] = (
                assessment["strict_satisfied"]
                and not hard_conflicts
                and hard_penalty <= 0
            )
            item["fit_penalty"] = assessment["demand_penalty"] + 2.5 * hard_penalty
            candidates.append(item)
    selected_tags = request.get("selected_tags") or []
    for item in candidates:
        model_available = item.get("model_evaluation_available") is not False
        price = _price_value(item)
        capability = center_capability(item)
        item["capability_score"] = round(capability, 3)
        item["predicted_price_wan"] = round(price, 3) if price is not None else None
        ce_capability=conservative_capability(item) if item.get('recommendation_price_wan') is not None else capability
        item["cost_effectiveness"] = round(ce_capability / max(effective_price(item),1e-9), 3) if price is not None else None
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
    prices = [effective_price(item) for item in candidates if item.get("predicted_price_wan") is not None]
    ces = [item["cost_effectiveness"] for item in candidates if item.get("cost_effectiveness") is not None]
    capabilities = [item["capability_score"] for item in candidates if item.get("capability_score") is not None]
    pmin, pmax = (min(prices), max(prices)) if prices else (0.0, 0.0)
    cemin, cemax = (min(ces), max(ces)) if ces else (0.0, 0.0)
    capmin, capmax = (min(capabilities), max(capabilities)) if capabilities else (0.0, 0.0)
    scenario_policy = request.get("_scenario_policy") or {}
    scenario_weights = scenario_policy.get("ranking_weights") or {}
    for item in candidates:
        numeric_price = effective_price(item) if item.get("predicted_price_wan") is not None else None
        numeric_ce = item.get("cost_effectiveness")
        # A missing price / cost-effectiveness is "unknown", not "best": give it
        # a neutral score instead of treating None as 0 (cheapest).
        price_score = 50.0 if numeric_price is None else (100.0 if pmax == pmin else 100.0 * (pmax - numeric_price) / (pmax - pmin))
        ce_score = 50.0 if numeric_ce is None else (100.0 if cemax == cemin else 100.0 * (numeric_ce - cemin) / (cemax - cemin))
        feasibility_score = 100.0 * float(item.get("feasibility_probability") if item.get("feasibility_probability") is not None else 0.75)
        uncertainty_width = float(item.get("score_uncertainty_width", (item.get("evaluation") or {}).get("score_uncertainty_width", 0)) or 0)
        uncertainty_penalty = min(10.0, 0.20 * uncertainty_width)
        item["price_score"] = round(price_score, 3)
        item["cost_effectiveness_score"] = round(ce_score, 3)
        item["uncertainty_penalty"] = round(uncertainty_penalty, 3)
        if item.get("model_evaluation_available") is False:
            base_score = 0.70 * item["tag_match_score"] + 0.30 * price_score - 20.0
        elif scenario_weights:
            numeric_capability = item.get("capability_score")
            capability_score = 50.0 if numeric_capability is None else (100.0 if capmax == capmin else 100.0 * (numeric_capability - capmin) / (capmax - capmin))
            demand_fit_score = max(0.0, 100.0 - min(100.0, 20.0 * float(item.get("fit_penalty", 0) or 0)))
            technical_score = 0.55 * demand_fit_score + 0.45 * item["tag_match_score"]
            base_score = (
                float(scenario_weights.get("technical", 0)) * technical_score +
                float(scenario_weights.get("price", 0)) * price_score +
                float(scenario_weights.get("capability", 0)) * capability_score
            ) - uncertainty_penalty
            item["technical_match_score"] = round(technical_score, 3)
            item["scenario_weighted_score"] = round(base_score, 3)
        else:
            base_score = (
                0.30 * item["tag_match_score"] + 0.25 * float(item.get("capability_score", 0)) +
                0.15 * price_score + 0.15 * ce_score + 0.15 * feasibility_score
            ) - uncertainty_penalty
        if item.get("best_effort"):
            base_score -= min(35.0, 4.0 * float(item.get("fit_penalty", 0) or 0))
        item["comprehensive_score"] = round(base_score, 3)
    trace = ranking_trace(request)
    key = trace["value_field"]
    reverse = trace["sort_direction"] == "desc"
    def _sort_value(item):
        # Fully-satisfied candidates rank first; within each satisfaction class the
        # demand gap (fit_penalty) decides, then the user's sort key, with missing
        # values always last.
        satisfied_rank = 0 if item.get("strict_filter_satisfied") else 1
        fit_rank = float(item.get("fit_penalty", 0) or 0)
        value = effective_price(item) if key=='predicted_price_wan' and item.get(key) is not None else item.get(key)
        if value is None:
            score_rank = float("inf")
        else:
            score_rank = float(value)
            if reverse:
                score_rank = -score_rank
        return (satisfied_rank, fit_rank, score_rank)
    candidates.sort(key=_sort_value)
    for index, item in enumerate(candidates, 1):
        item["rank"] = index
        item["ranking_trace"] = dict(trace)
    return candidates


def rank_historical_products(items, request, tag_weights, definitions=None, tag_map=None, constraint_rules=None):
    """Rank stored history as a soft recommendation without inventing model outputs.

    Used when either independent HTTP model service is unavailable.  No history
    row is dropped for failing a user threshold; every row keeps a requirement
    assessment (price / attributes / tags are judgeable offline, model outputs
    become ``unknown``) and fully-satisfied rows rank first.
    """
    from .requirement_assessment import assess_requirements
    definitions = definitions or {}
    tag_map = tag_map or {}
    selected_tags = list(request.get("selected_tags") or [])
    candidates = []
    for source in items:
        item = dict(source)
        price = _number(item.get("historical_price_wan"))
        item["predicted_price_wan"] = price
        item["capability_score"] = None
        item["conservative_capability_score"] = None
        item["cost_effectiveness"] = None
        item["feasibility_probability"] = None
        item["model_evaluation_available"] = False
        item["recommendation_confidence"] = "unavailable"
        item["requirement_assessment"] = assess_requirements(item, request, definitions, tag_map, constraint_rules)
        item["strict_filter_satisfied"] = item["requirement_assessment"]["strict_satisfied"]
        item["fit_penalty"] = item["requirement_assessment"]["demand_penalty"]
        own = set(item.get("tags") or [])
        item["matched_tags"] = [tag for tag in selected_tags if tag in own]
        item["missing_tags"] = [tag for tag in selected_tags if tag not in own]
        item["tag_match_score"] = round(compute_tag_match(item, selected_tags, tag_weights), 3)
        candidates.append(item)
    if not candidates:
        return []
    prices = [item["predicted_price_wan"] for item in candidates if item["predicted_price_wan"] is not None]
    pmin, pmax = (min(prices), max(prices)) if prices else (0.0, 0.0)
    scenario_policy = request.get("_scenario_policy") or {}
    scenario_weights = scenario_policy.get("ranking_weights") or {}
    for item in candidates:
        price = item["predicted_price_wan"]
        price_score = 50.0 if price is None else 100.0 if pmax == pmin else 100.0 * (pmax - price) / (pmax - pmin)
        item["price_score"] = round(price_score, 3)
        if scenario_weights:
            technical = max(0.0, 100.0 - min(100.0, 20.0 * float(item.get("fit_penalty", 0) or 0)))
            technical_weight = float(scenario_weights.get("technical", 0)) + float(scenario_weights.get("capability", 0))
            price_weight = float(scenario_weights.get("price", 0))
            total_weight = max(technical_weight + price_weight, 1e-9)
            item["comprehensive_score"] = round((technical_weight * technical + price_weight * price_score) / total_weight, 3)
            item["technical_match_score"] = round(technical, 3)
            item["scenario_weighted_score"] = item["comprehensive_score"]
        else:
            item["comprehensive_score"] = round(0.70 * item["tag_match_score"] + 0.30 * price_score, 3)
    trace = ranking_trace(request)
    key = trace["value_field"]
    reverse = trace["sort_direction"] == "desc"
    def _sort_value(item):
        # Fully-satisfied rows first; within each class the demand gap decides,
        # then the sort key, with missing values always last regardless of direction.
        satisfied_rank = 0 if item.get("strict_filter_satisfied") else 1
        fit_rank = float(item.get("fit_penalty", 0) or 0)
        value = item.get(key)
        if value is None:
            score_rank = float("inf")
        else:
            score_rank = float(value)
            if reverse:
                score_rank = -score_rank
        return (satisfied_rank, fit_rank, score_rank)
    candidates.sort(key=_sort_value)
    for index, item in enumerate(candidates, 1):
        item["rank"] = index
        item["ranking_trace"] = dict(trace)
    return candidates
