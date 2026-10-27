# -*- coding: utf-8 -*-
"""Deterministic, candidate-specific recommendation explanations."""
from __future__ import division

from .value_semantics import business_display_value


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _rank_map(items, field, reverse=False):
    indexed = [(index, _number(item.get(field))) for index, item in enumerate(items)]
    ordered = sorted((pair for pair in indexed if pair[1] is not None), key=lambda pair: pair[1], reverse=reverse)
    return dict((index, rank) for rank, (index, _value) in enumerate(ordered, 1))


def _percent(delta):
    return "%.1f%%" % (100.0 * delta)


def _candidate_options(item, index, items, scenario):
    price = _number(item.get("predicted_price_wan"))
    capability = _number(item.get("capability_score"))
    ce = _number(item.get("cost_effectiveness"))
    technical = _number(item.get("technical_match_score"))
    valid_prices = [_number(x.get("predicted_price_wan")) for x in items if _number(x.get("predicted_price_wan")) is not None]
    valid_caps = [_number(x.get("capability_score")) for x in items if _number(x.get("capability_score")) is not None]
    min_price = min(valid_prices) if valid_prices else None
    max_cap = max(valid_caps) if valid_caps else None
    cheapest = next((x for x in items if min_price is not None and _number(x.get("predicted_price_wan")) == min_price), None)
    strongest = next((x for x in items if max_cap is not None and _number(x.get("capability_score")) == max_cap), None)
    options = []

    def add(priority, code, title, summary):
        options.append((priority, code, title, summary))

    if scenario == "cost":
        if item.get("price_rank") == 1:
            add(100, "lowest_price", "当前候选中价格最低", "在当前可比较方案中预测价格最低。")
        if price is not None and min_price and cheapest and capability is not None:
            cheapest_cap = _number(cheapest.get("capability_score"))
            premium = price / min_price - 1.0
            if index != items.index(cheapest) and premium <= 0.12 and cheapest_cap is not None and capability >= cheapest_cap + 2.0:
                add(92, "near_low_price_more_capability", "价格接近最低，效能更高", "价格比最低方案高%s，但效能提高%.1f分。" % (_percent(premium), capability - cheapest_cap))
        if item.get("technical_match_rank") == 1 and price is not None and min_price and price <= min_price * 1.20:
            add(84, "low_price_best_technical", "低成本且技术匹配完整", "价格处于低位，同时技术要求匹配表现最好。")
        if item.get("cost_effectiveness_rank") == 1:
            add(76, "best_ce", "低成本方案中效费比突出", "单位成本获得的效能表现位于当前候选首位。")
    elif scenario == "performance":
        if item.get("capability_rank") == 1:
            add(100, "highest_capability", "当前效能最高", "在当前可比较候选中效能评分最高。")
        if capability is not None and max_cap is not None and strongest and price is not None:
            strongest_price = _number(strongest.get("predicted_price_wan"))
            gap = max_cap - capability
            if index != items.index(strongest) and gap <= 3.0 and strongest_price and price <= strongest_price * 0.95:
                add(92, "near_high_capability_cheaper", "接近最高效能，但价格更低", "效能仅低%.1f分，预测价格低%s。" % (gap, _percent(1.0 - price / strongest_price)))
        if item.get("technical_match_rank") == 1 and capability is not None and max_cap is not None and capability >= max_cap - 5.0:
            add(84, "high_capability_best_technical", "高效能且需求匹配完整", "效能处于前列，同时技术要求匹配表现最好。")
        if item.get("cost_effectiveness_rank") == 1:
            add(76, "high_capability_value", "高效能方向中效费比突出", "在保持效能优势的同时，单位成本产出更高。")
    else:
        if item.get("cost_effectiveness_rank") == 1:
            add(100, "best_ce", "效费比表现最佳", "在当前候选中，单位成本获得的效能最高。")
        if capability is not None and max_cap is not None and strongest and price is not None:
            strongest_price = _number(strongest.get("predicted_price_wan"))
            gap = max_cap - capability
            if index != items.index(strongest) and gap <= 3.0 and strongest_price and price <= strongest_price * 0.92:
                add(94, "near_high_capability_cheaper", "性能接近最高，成本更低", "效能仅低%.1f分，但预测价格低%s。" % (gap, _percent(1.0 - price / strongest_price)))
        if price is not None and min_price and cheapest and capability is not None:
            cheapest_cap = _number(cheapest.get("capability_score"))
            premium = price / min_price - 1.0
            if index != items.index(cheapest) and premium <= 0.08 and cheapest_cap is not None and capability >= cheapest_cap + 3.0:
                add(90, "near_low_price_more_capability", "价格接近最低，效能明显更高", "价格仅高%s，效能提高%.1f分。" % (_percent(premium), capability - cheapest_cap))
        if item.get("technical_match_rank") == 1:
            add(84, "best_technical", "技术要求匹配最完整", "对当前技术条件和功能偏好的覆盖表现最好。")
        if item.get("price_rank") == 1:
            add(78, "lowest_price", "成本控制表现突出", "当前候选中预测价格最低。")
        if item.get("capability_rank") == 1:
            add(76, "highest_capability", "效能表现突出", "当前候选中效能评分最高。")
    if technical is not None and item.get("technical_match_rank") == 1:
        add(60, "best_technical", "需求匹配表现突出", "当前技术要求与功能偏好的匹配度最高。")
    if not options:
        rank = int(item.get("rank") or (index + 1))
        add(10, "scenario_fit_rank_%d" % rank, "综合比较位列第%d" % rank,
            "根据需求满足程度和当前优化目标综合比较后进入本次推荐。")
    return sorted(options, key=lambda value: -value[0])


def _condition_text(condition, definitions=None):
    label = str(condition.get("label") or condition.get("key") or "当前要求")
    actual = condition.get("actual")
    target = condition.get("target")
    # Never expose the normalised ranking gap as a physical-world distance.
    # Older records without business_gap simply omit a numeric difference.
    business_gap = _number(condition.get("business_gap"))
    if actual not in (None, "") and target not in (None, ""):
        definition = (definitions or {}).get(condition.get("parameter_id") or condition.get("key"))
        actual_text = business_display_value(actual, definition) if definition else str(actual)
        target_text = business_display_value(target, definition) if definition else str(target)
        unit = str(condition.get("unit") or ((definition or {}).get("unit") if definition else "") or "")
        suffix = "当前值%s%s，目标%s%s" % (actual_text, unit, target_text, unit)
        if business_gap is not None and business_gap > 0:
            operator = condition.get("operator")
            direction = "高于要求" if operator in ("lte", "lt") else "低于要求" if operator in ("gte", "gt") else "与要求相差"
            suffix += "，%s%.3g%s" % (direction, business_gap, unit)
        return label, suffix + "。"
    return label, "该项尚未满足，其它条件仍按实际结果比较。"


def _non_strict_options(item, index, items, definitions=None):
    """Build truthful, candidate-specific options for Best Effort/Exploratory."""
    options = []

    def add(priority, code, title, summary):
        options.append((priority, code, title, summary))

    direction = item.get("solution_direction") or {}
    branch_id = direction.get("branch_id")
    if branch_id and branch_id != "BRANCH-ALL":
        add(110, "direction_%s" % branch_id, direction.get("title") or "代表一条需求方向",
            direction.get("summary") or "该方案围绕这一需求方向进行探索。")

    assessment = item.get("requirement_assessment") or {}
    unmet = [condition for condition in assessment.get("conditions") or [] if condition.get("status") == "unmatched"]
    if unmet:
        closest = min(unmet, key=lambda condition: _number(condition.get("normalized_gap")) if _number(condition.get("normalized_gap")) is not None else (_number(condition.get("gap")) if _number(condition.get("gap")) is not None else 1e9))
        label, summary = _condition_text(closest, definitions)
        add(100, "closest_%s" % (closest.get("key") or index), "最接近%s" % label, summary)
        matched_count = int(assessment.get("matched_count") or 0)
        total_count = int(assessment.get("total_count") or 0)
        if matched_count and total_count:
            add(86, "coverage_%d_of_%d" % (matched_count, total_count), "已满足%d项主要要求" % matched_count,
                "当前共核对%d项要求，仍有%d项需要调整。" % (total_count, int(assessment.get("unmatched_count") or 0)))

    if item.get("price_rank") == 1 and _number(item.get("predicted_price_wan")) is not None:
        add(78, "best_effort_lowest_price", "尽力方案中成本最低", "在未完全满足条件的候选中预测价格最低。")
    if item.get("capability_rank") == 1 and _number(item.get("capability_score")) is not None:
        add(76, "best_effort_highest_capability", "尽力方案中效能最高", "在未完全满足条件的候选中效能评分最高。")
    rank = int(item.get("rank") or (index + 1))
    add(10, "exploration_rank_%d" % rank, "探索方向位列第%d" % rank,
        "该参数组合代表当前条件下可继续复核的一条调整路线。")
    return sorted(options, key=lambda value: -value[0])


def annotate_candidate_recommendations(items, scenario_policy, definitions=None):
    """Attach relative ranks and one concise, truthful reason to each item."""
    items = list(items or [])
    ranks = {
        "price_rank": _rank_map(items, "predicted_price_wan", reverse=False),
        "capability_rank": _rank_map(items, "capability_score", reverse=True),
        "cost_effectiveness_rank": _rank_map(items, "cost_effectiveness", reverse=True),
        "technical_match_rank": _rank_map(items, "technical_match_score", reverse=True),
        "tag_match_rank": _rank_map(items, "tag_match_score", reverse=True),
    }
    scenario = str((scenario_policy or {}).get("scenario") or "balanced")
    used = set()
    for index, item in enumerate(items):
        for field, mapping in ranks.items():
            item[field] = mapping.get(index)
        if not item.get("strict_filter_satisfied") or (
            item.get("model_evaluation_available") is False and item.get("is_generated")
        ):
            options = _non_strict_options(item, index, items, definitions)
            chosen = next((value for value in options if value[1] not in used), options[0])
            used.add(chosen[1])
            reason = {"code": chosen[1], "title": chosen[2], "summary": chosen[3]}
        else:
            options = _candidate_options(item, index, items, scenario)
            chosen = next((value for value in options if value[1] not in used), options[0])
            used.add(chosen[1])
            reason = {"code": chosen[1], "title": chosen[2], "summary": chosen[3]}
        item["recommendation_reason"] = reason
        item["scenario_recommendation"] = reason["title"]
        item["scenario_recommendation_detail"] = reason["summary"]
    return items
