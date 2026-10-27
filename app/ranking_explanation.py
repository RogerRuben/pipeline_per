# -*- coding: utf-8 -*-
"""Deterministic explanations derived from the actual ranked candidate list."""
from __future__ import print_function

from .value_semantics import business_display_value


def _num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fmt(value):
    return ("%.3f" % float(value)).rstrip("0").rstrip(".")


def _condition_map(item):
    assessment = item.get("requirement_assessment") or {}
    return dict((str(c.get("key")), c) for c in assessment.get("conditions") or [])


def annotate_ranking_explanations(items, request, definitions=None):
    definitions = definitions or {}
    result = list(items or [])
    for index, item in enumerate(result):
        trace = item.get("ranking_trace") or {}
        if index + 1 >= len(result):
            item["ranking_explanation"] = {
                "rank": index + 1, "compared_to": None,
                "title": "当前结果中的最后一个可比较方案",
                "summary": "该方案仍按当前需求满足程度和排序目标进入结果。", "factors": []}
            continue
        other = result[index + 1]
        factors = []
        strict_a, strict_b = bool(item.get("strict_filter_satisfied")), bool(other.get("strict_filter_satisfied"))
        if strict_a != strict_b:
            factors.append({"kind": "requirement", "advantage": strict_a,
                            "text": "完整满足当前需求" if strict_a else "仍有未满足的当前需求"})
        amap, bmap = _condition_map(item), _condition_map(other)
        for key, condition in amap.items():
            peer = bmap.get(key)
            if not peer or condition.get("matched") == peer.get("matched"):
                continue
            if condition.get("matched"):
                gap = peer.get("business_gap")
                label = condition.get("label") or definitions.get(key, {}).get("label") or key
                unit = condition.get("unit") or definitions.get(key, {}).get("unit") or ""
                text = "%s满足要求" % label
                if gap is not None:
                    text += "，下一名相差%s%s" % (_fmt(gap), unit)
                else:
                    definition = definitions.get(key) or {}
                    text += "（本方案：%s；下一名：%s）" % (
                        business_display_value(condition.get("actual"), definition),
                        business_display_value(peer.get("actual"), definition),
                    )
                factors.append({"kind": "condition", "advantage": True, "text": text,
                                "business_gap": gap, "unit": unit})
                break
        price_a, price_b = _num(item.get("predicted_price_wan")), _num(other.get("predicted_price_wan"))
        cap_a, cap_b = _num(item.get("capability_score")), _num(other.get("capability_score"))
        if price_a is not None and price_b is not None and abs(price_a - price_b) > 1e-9:
            factors.append({"kind": "price", "advantage": price_a < price_b,
                            "text": "价格%s %s 万元" % ("低" if price_a < price_b else "高", _fmt(abs(price_a-price_b))),
                            "business_delta": abs(price_a-price_b), "unit": "万元"})
        if cap_a is not None and cap_b is not None and abs(cap_a - cap_b) > 1e-9:
            factors.append({"kind": "capability", "advantage": cap_a > cap_b,
                            "text": "效能%s %s 分" % ("高" if cap_a > cap_b else "低", _fmt(abs(cap_a-cap_b))),
                            "business_delta": abs(cap_a-cap_b), "unit": "分"})
        fit_a, fit_b = _num(item.get("fit_penalty")), _num(other.get("fit_penalty"))
        if strict_a != strict_b:
            precedence = "当前排序首先考虑需求是否完整满足"
        elif fit_a is not None and fit_b is not None and abs(fit_a-fit_b) > 1e-9:
            precedence = "需求满足程度相同时，当前方案与需求的总体差距更小"
        else:
            direction = "从低到高" if trace.get("sort_direction") == "asc" else "从高到低"
            source = "（用户调整）" if trace.get("sort_source") == "user_override" else ""
            precedence = "当前按%s%s%s排序" % (
                trace.get("sort_display_name") or "综合推荐", direction, source)
        item["ranking_explanation"] = {
            "rank": index + 1,
            "compared_to": other.get("agreement_id") or other.get("id"),
            "title": "为什么排在第 %d 名前面？" % (index + 2),
            "summary": "%s，因此该方案排在下一方案之前。" % precedence,
            "factors": factors[:4],
        }
    return result
