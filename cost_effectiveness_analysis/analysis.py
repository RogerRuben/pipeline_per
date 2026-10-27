# -*- coding: utf-8 -*-
from __future__ import print_function


def number(value):
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cost_effectiveness(price, effectiveness):
    price, effectiveness = number(price), number(effectiveness)
    if price is None or effectiveness is None or price <= 0:
        return None
    return effectiveness / price


def pareto_ids(items):
    valid = []
    for item in items or []:
        price = number(item.get("predicted_price_wan"))
        effect = number(item.get("capability_score"))
        if price is not None and effect is not None:
            valid.append((str(item.get("scheme_id")), price, effect))
    result = []
    for scheme_id, price, effect in valid:
        dominated = any(
            other_price <= price and other_effect >= effect
            and (other_price < price or other_effect > effect)
            for other_id, other_price, other_effect in valid
            if other_id != scheme_id
        )
        if not dominated:
            result.append(scheme_id)
    return result


def summarize(items):
    eligible = [x for x in items if number(x.get("predicted_price_wan")) is not None
                and number(x.get("capability_score")) is not None]
    ce_items = [x for x in eligible if number(x.get("cost_effectiveness")) is not None]
    def chosen(values, key, reverse=False):
        if not values:
            return None
        return sorted(values, key=lambda x: number(x.get(key)), reverse=reverse)[0].get("scheme_id")
    return {
        "scheme_count": len(items),
        "valid_scheme_count": len(eligible),
        "pareto_count": sum(1 for x in items if x.get("pareto") is True),
        "lowest_price_scheme_id": chosen(eligible, "predicted_price_wan"),
        "highest_effectiveness_scheme_id": chosen(eligible, "capability_score", True),
        "highest_cost_effectiveness_scheme_id": chosen(ce_items, "cost_effectiveness", True),
    }


def apply_analysis(items):
    result = []
    for source in items or []:
        item = dict(source)
        price, effect = number(item.get("predicted_price_wan")), number(item.get("capability_score"))
        item["cost_effectiveness"] = cost_effectiveness(price, effect)
        item["invalid_price_for_ce"] = price is not None and price <= 0
        item["pareto"] = None
        result.append(item)
    frontier = set(pareto_ids(result))
    for item in result:
        if number(item.get("predicted_price_wan")) is not None and number(item.get("capability_score")) is not None:
            item["pareto"] = item["scheme_id"] in frontier
    return result, [item["scheme_id"] for item in result if item.get("pareto") is True]


def baseline_differences(items, baseline_scheme_id):
    base = next((x for x in items if x.get("scheme_id") == baseline_scheme_id), None)
    if not base:
        return dict((x.get("scheme_id"), None) for x in items)
    keys = ("predicted_price_wan", "capability_score", "cost_effectiveness")
    output = {}
    for item in items:
        delta = {}
        for key in keys:
            current, base_value = number(item.get(key)), number(base.get(key))
            delta[key] = None if current is None or base_value is None else current - base_value
        output[item.get("scheme_id")] = delta
    return output
