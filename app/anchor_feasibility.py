# -*- coding: utf-8 -*-
"""Explicit technical-filter feasibility and anchor-integrity diagnostics."""
from __future__ import print_function

from .value_semantics import normalize_numeric
from .recommender import filter_match

_POS_INF = float("inf")
_NEG_INF = float("-inf")


def _num(value):
    result = normalize_numeric(value)
    return result if result is not None else None


def _finite(value):
    """Return the value if finite, otherwise None (for public JSON payloads)."""
    if value is None:
        return None
    number = float(value)
    if number in (_POS_INF, _NEG_INF) or number != number:  # NaN check
        return None
    return number


def _conflict(key, definition, op, lower, lower_inclusive, upper, upper_inclusive,
              engineering_min, engineering_max, closest, reason, requested_value=None):
    return {
        "parameter_id": key,
        "label": definition.get("label", key),
        "operator": op,
        "requested_min": _finite(lower),
        "requested_min_inclusive": bool(lower_inclusive),
        "requested_max": _finite(upper),
        "requested_max_inclusive": bool(upper_inclusive),
        "requested_value": _finite(requested_value),
        "engineering_min": engineering_min,
        "engineering_max": engineering_max,
        "closest_feasible_value": _finite(closest),
        "reason": reason,
    }


def _merge_requested_interval(rules, definitions):
    """Intersect AND constraints for one parameter into a requested interval.

    Returns ``(lower, lower_inclusive, upper, upper_inclusive, conflict)``.
    ``conflict`` is a dict when the user's own conditions are mutually
    inconsistent.  The merge is order-independent: constraints are intersected,
    never assigned over previous bounds.
    """
    lower = _NEG_INF
    lower_inclusive = True
    upper = _POS_INF
    upper_inclusive = True
    conflict = None
    key = None
    definition = {}

    for rule in rules or []:
        key = rule.get("parameter_id")
        definition = definitions.get(key) or {}
        op = rule.get("operator")
        v1 = _num(rule.get("value1"))
        v2 = _num(rule.get("value2"))

        if op in ("gte", "gt") and v1 is not None:
            new_inclusive = op == "gte"
            if v1 > lower or (v1 == lower and not new_inclusive and lower_inclusive):
                lower = v1
                lower_inclusive = new_inclusive
            elif v1 == lower:
                # Any strict > makes the shared lower bound exclusive.
                lower_inclusive = lower_inclusive and new_inclusive
        elif op in ("lte", "lt") and v1 is not None:
            new_inclusive = op == "lte"
            if v1 < upper or (v1 == upper and not new_inclusive and upper_inclusive):
                upper = v1
                upper_inclusive = new_inclusive
            elif v1 == upper:
                upper_inclusive = upper_inclusive and new_inclusive
        elif op == "eq" and v1 is not None:
            # Intersect with the exact point, never overwrite other bounds.
            if v1 > lower or (v1 == lower and not lower_inclusive):
                lower = v1
                lower_inclusive = True
            else:
                lower_inclusive = lower_inclusive and True
            if v1 < upper or (v1 == upper and not upper_inclusive):
                upper = v1
                upper_inclusive = True
            else:
                upper_inclusive = upper_inclusive and True
        elif op == "range_inside" and v1 is not None and v2 is not None:
            lo, hi = min(v1, v2), max(v1, v2)
            if lo > lower or (lo == lower and not lower_inclusive):
                lower = lo
                lower_inclusive = True
            else:
                lower_inclusive = lower_inclusive and True
            if hi < upper or (hi == upper and not upper_inclusive):
                upper = hi
                upper_inclusive = True
            else:
                upper_inclusive = upper_inclusive and True

    if lower > upper or (lower == upper and not (lower_inclusive and upper_inclusive)):
        conflict = _conflict(
            key, definition, "and",
            lower, lower_inclusive, upper, upper_inclusive,
            None, None, None, "explicit_filters_mutually_inconsistent",
        )
    return lower, lower_inclusive, upper, upper_inclusive, conflict


def assess_explicit_filter_feasibility(indicator_filters, definitions, mode="all"):
    """Describe explicit filters against advisory DataMaster min/max metadata.

    AND conditions on the same parameter are merged before comparing with the
    engineering domain, so ``weight>=6 AND weight<=4`` is detected as mutually
    inconsistent instead of passing the per-rule check.
    """
    definitions = definitions or {}
    filters = list(indicator_filters or [])
    conflicts = []

    if mode != "all":
        # Keep the simple per-rule path for OR mode; complex multi-condition OR
        # is intentionally out of scope for this micro patch.
        single_results = []
        for rule in filters:
            key = rule.get("parameter_id")
            definition = definitions.get(key) or {}
            raw_min = definition.get("min_value")
            raw_max = definition.get("max_value")
            if raw_min is None or raw_max is None:
                continue
            engineering_min = float(raw_min)
            engineering_max = float(raw_max)
            op = rule.get("operator")
            v1 = _num(rule.get("value1"))
            conflict = None
            if op in ("lte", "lt") and v1 is not None and (v1 < engineering_min or (op == "lt" and v1 <= engineering_min)):
                conflict = _conflict(
                    key, definition, op, _NEG_INF, True, v1, op == "lte",
                    engineering_min, engineering_max, engineering_min,
                    "requested_upper_below_engineering_min", requested_value=v1,
                )
            elif op in ("gte", "gt") and v1 is not None and (v1 > engineering_max or (op == "gt" and v1 >= engineering_max)):
                conflict = _conflict(
                    key, definition, op, v1, op == "gte", _POS_INF, True,
                    engineering_min, engineering_max, engineering_max,
                    "requested_lower_above_engineering_max", requested_value=v1,
                )
            if conflict:
                conflicts.append(conflict)
                single_results.append(False)
            else:
                single_results.append(True)
        return {
            "strictly_feasible": True,
            "conflicts": conflicts,
            "blocking_conflicts": [],
            "advisory_only": True,
            "range_role": "data_master_reference",
        }

    groups = {}
    for rule in filters:
        key = rule.get("parameter_id")
        if not key or key not in definitions:
            continue
        groups.setdefault(key, []).append(rule)

    for key, rules in groups.items():
        definition = definitions.get(key) or {}
        raw_min = definition.get("min_value")
        raw_max = definition.get("max_value")
        if raw_min is None or raw_max is None:
            continue
        engineering_min = float(raw_min)
        engineering_max = float(raw_max)
        lower, lower_inclusive, upper, upper_inclusive, mutual_conflict = _merge_requested_interval(rules, definitions)
        if mutual_conflict is not None:
            mutual_conflict["engineering_min"] = engineering_min
            mutual_conflict["engineering_max"] = engineering_max
            conflicts.append(mutual_conflict)
            continue

        # Strict boundary semantics: a strict bound exactly on the engineering
        # boundary has no intersection.
        below = upper < engineering_min or (upper == engineering_min and not upper_inclusive)
        above = lower > engineering_max or (lower == engineering_max and not lower_inclusive)
        if below:
            conflicts.append(_conflict(
                key, definition, "and",
                lower, lower_inclusive, upper, upper_inclusive,
                engineering_min, engineering_max, engineering_min,
                "requested_upper_below_engineering_min",
            ))
        elif above:
            conflicts.append(_conflict(
                key, definition, "and",
                lower, lower_inclusive, upper, upper_inclusive,
                engineering_min, engineering_max, engineering_max,
                "requested_lower_above_engineering_max",
            ))

    blocking = [item for item in conflicts if item.get("reason") == "explicit_filters_mutually_inconsistent"]
    return {
        "strictly_feasible": not blocking,
        "conflicts": conflicts,
        "blocking_conflicts": blocking,
        "advisory_only": not blocking,
        "range_role": "data_master_reference",
    }


def validate_anchor_integrity(params, indicator_filters, definitions, mode="all"):
    """Return violations using the same canonical matcher as recommendation.

    DataMaster ranges are advisory, so no explicit condition is exempted merely
    because it lies outside those ranges.
    """
    checks = []
    for rule in indicator_filters or []:
        key = rule.get("parameter_id")
        ok = filter_match(params, rule, definitions.get(key))
        checks.append((key, ok))

    if mode == "any":
        violations = [] if any(ok for _key, ok in checks) else [
            {"stage": "anchor_integrity", "parameter_id": key, "requested": rule, "actual": params.get(key)}
            for key, ok in checks if not ok
        ]
    else:
        violations = [
            {"stage": "anchor_integrity", "parameter_id": key, "requested": rule, "actual": params.get(key)}
            for key, ok in checks if not ok
        ]
    return violations
