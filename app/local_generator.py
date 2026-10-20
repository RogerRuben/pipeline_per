# -*- coding: utf-8 -*-
"""Demand-anchored, reverse-contour, adaptive neighborhood generator.

The generator is product agnostic. Product attributes, tags, coupling rules and
constraints come from the runtime database/DataMaster. Historical protocols are
used as explainable starting points. User-specified technical conditions are
projected first and then kept fixed. Learned feasible contours are soft empirical
references: they guide compensation through other adjustable attributes and
produce extrapolation warnings, but never silently overwrite a user target.

Search outline
--------------
1. Select several similar and diverse historical seeds.
2. Project direct user/tag conditions onto every seed and lock those attributes.
3. Evaluate contour deviation for locked targets.
4. Search the remaining attributes with finite-difference reverse-contour moves,
   one/two-variable neighborhoods and correlated local perturbations.
5. Repair explicit DataMaster hard relations without changing locked targets.
6. Evaluate price, effectiveness, feasibility and anomaly status for every new
   combination.
7. Iteratively promote better neighbors as new search centres.
8. Return demand-satisfying schemes even when they are outside the historical
   contour, with explicit extrapolation and confidence warnings.
"""
from __future__ import print_function

import json
import math
import random
import time

from .recommender import filter_match, rank_agreements, effective_price


def _float(value, default=None):
    try:
        return float(str(value).strip().upper().replace("IP", ""))
    except (TypeError, ValueError):
        return default


def _truth(value):
    return str(value).strip().lower() in ("1", "true", "yes", "有", "是") or value is True or value == 1


def _clamp(value, lo, hi):
    return max(float(lo), min(float(hi), float(value)))


def _mean(values):
    return sum(values) / max(len(values), 1)


def _std(values):
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    return math.sqrt(sum((x - mean) ** 2 for x in values) / (len(values) - 1))


def _cholesky(matrix):
    n = len(matrix)
    out = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1):
            value = matrix[i][j] - sum(out[i][k] * out[j][k] for k in range(j))
            if i == j:
                out[i][j] = math.sqrt(max(value, 1e-8))
            else:
                out[i][j] = value / max(out[j][j], 1e-8)
    return out


def _mat_vec(lower, vector):
    return [sum(lower[i][j] * vector[j] for j in range(i + 1)) for i in range(len(vector))]


def merge_bounds(base, extra):
    """Merge direct demand bounds while retaining contradiction metadata."""
    result = {}
    for source in (base or {}, extra or {}):
        for key, value in source.items():
            current = result.setdefault(key, {})
            if value.get("min") is not None:
                current["min"] = max(float(value["min"]), float(current.get("min", value["min"])))
            if value.get("max") is not None:
                current["max"] = min(float(value["max"]), float(current.get("max", value["max"])))
            if value.get("allowed") is not None:
                values = list(value["allowed"])
                current["allowed"] = values if "allowed" not in current else [x for x in current["allowed"] if x in values]
    for item in result.values():
        lower = item.get("min", -1e99)
        upper = item.get("max", 1e99)
        if lower > upper:
            item["conflict"] = True
            item["requested_min"] = lower
            item["requested_max"] = upper
            compromise = (float(lower) + float(upper)) / 2.0
            item["min"] = compromise
            item["max"] = compromise
        if "allowed" in item and not item["allowed"]:
            item["conflict"] = True
    return result


def filters_to_bounds(filters, mode="all"):
    # OR filters cannot safely be reduced to one joint generation box.
    if mode != "all":
        return {}
    result = {}
    for rule in filters or []:
        key, op = rule.get("parameter_id"), rule.get("operator")
        if not key:
            continue
        item = result.setdefault(key, {})
        v1, v2 = _float(rule.get("value1")), _float(rule.get("value2"))
        if op == "gte" and v1 is not None:
            item["min"] = max(v1, float(item.get("min", v1)))
        elif op == "gt" and v1 is not None:
            item["min"] = max(v1 + 1e-7, float(item.get("min", v1 + 1e-7)))
        elif op == "lte" and v1 is not None:
            item["max"] = min(v1, float(item.get("max", v1)))
        elif op == "lt" and v1 is not None:
            item["max"] = min(v1 - 1e-7, float(item.get("max", v1 - 1e-7)))
        elif op == "eq" and v1 is not None:
            item["min"] = v1
            item["max"] = v1
        elif op == "boolean_is":
            item["min"] = item["max"] = 1.0 if _truth(rule.get("value1")) else 0.0
        elif op == "range_inside" and v1 is not None and v2 is not None:
            item["min"], item["max"] = min(v1, v2), max(v1, v2)
        elif not item:
            result.pop(key, None)
    return result


class HistorySeededGenerator(object):
    def __init__(self, store, runtime, evaluate_callback, evaluate_batch_callback=None):
        self.store = store
        self.runtime = runtime
        self.evaluate_callback = evaluate_callback
        self.evaluate_batch_callback = evaluate_batch_callback

    def _parameter_definitions(self):
        return self.store.parameter_map()

    def _request_distance(self, item, request, definitions, tag_weights):
        params = item.get("params") or {}
        distance = 0.0
        for tag in request.get("selected_tags") or []:
            if tag not in set(item.get("tags") or []):
                distance += 1.5 * float(tag_weights.get(tag, 1.0))
        for rule in request.get("indicator_filters") or []:
            key = rule.get("parameter_id")
            if key not in params:
                distance += 4.0
                continue
            if filter_match(params, rule):
                continue
            definition = definitions.get(key, {})
            span = max(float(definition.get("max_value") or 1) - float(definition.get("min_value") or 0), 1e-9)
            actual, target = _float(params.get(key)), _float(rule.get("value1"))
            distance += abs(actual - target) / span if actual is not None and target is not None else 1.0
        evaluation = item.get("evaluation") or {}
        checks = [
            ("max_price", effective_price(evaluation), "max"),
            ("min_capability", evaluation.get("conservative_capability_score", evaluation.get("capability_score")), "min"),
            ("min_cost_effectiveness", evaluation.get("cost_effectiveness"), "min"),
            ("min_feasibility", evaluation.get("feasibility_probability"), "min"),
        ]
        for req_key, actual, direction in checks:
            target = request.get(req_key)
            if target in (None, "") or actual in (None, ""):
                continue
            target, actual = float(target), float(actual)
            if direction == "max" and actual > target:
                distance += (actual - target) / max(abs(target), 1.0)
            if direction == "min" and actual < target:
                distance += (target - actual) / max(abs(target), 1.0)
        return distance

    def select_seeds(self, request, limit=12, historical=None):
        historical = list(historical if historical is not None else self.store.historical_agreements())
        definitions = self._parameter_definitions()
        tag_weights = dict((key, value.get("weight", 1.0)) for key, value in self.store.tag_map().items())
        scored = [(self._request_distance(item, request, definitions, tag_weights), item) for item in historical]
        scored.sort(key=lambda x: x[0])
        selected = []
        for _score, item in scored:
            if not selected:
                selected.append(item)
            elif min(self._normalized_distance(item["params"], x["params"], definitions) for x in selected) >= 0.06 or len(selected) < 4:
                selected.append(item)
            if len(selected) >= min(limit, len(scored)):
                break
        return selected or [x[1] for x in scored[:limit]]

    def _normalized_distance(self, a, b, definitions):
        values = []
        for key, definition in definitions.items():
            if key not in a or key not in b:
                continue
            kind = self._search_type(definition)
            if kind in ("boolean", "unordered_enum"):
                values.append(0.0 if self._value_equal(a[key], b[key], definition) else 1.0)
            elif kind == "ordered_discrete":
                allowed = self._normalized_allowed_values(definition)
                if allowed:
                    try:
                        ia = min(range(len(allowed)), key=lambda i: abs(float(allowed[i]) - float(a[key])))
                        ib = min(range(len(allowed)), key=lambda i: abs(float(allowed[i]) - float(b[key])))
                        values.append(abs(ia - ib) / max(len(allowed) - 1, 1))
                    except Exception:
                        values.append(0.0 if self._value_equal(a[key], b[key], definition) else 1.0)
                else:
                    values.append(0.0 if self._value_equal(a[key], b[key], definition) else 1.0)
            else:
                span = max(float(definition.get("max_value") or 1) - float(definition.get("min_value") or 0), 1e-9)
                values.append(abs(float(a[key]) - float(b[key])) / span)
        return math.sqrt(sum(x * x for x in values) / max(len(values), 1))

    def _local_statistics(self, seeds, definitions):
        numeric = [
            key for key, d in definitions.items()
            if d.get("enabled", 1) and d.get("auto_adjustable", 1)
            and self._search_type(d) in ("continuous", "integer")
        ]
        means, stds = {}, {}
        for key in numeric:
            vals = [float(item["params"][key]) for item in seeds if item.get("params", {}).get(key) not in (None, "")]
            means[key] = _mean(vals)
            span = float(definitions[key].get("max_value") or 1) - float(definitions[key].get("min_value") or 0)
            stds[key] = max(_std(vals), span * 0.025)
        corr = [[0.0] * len(numeric) for _ in numeric]
        for i, key_i in enumerate(numeric):
            corr[i][i] = 1.0
            for j in range(i):
                key_j = numeric[j]
                pairs = [
                    (float(x["params"][key_i]), float(x["params"][key_j]))
                    for x in seeds if key_i in x.get("params", {}) and key_j in x.get("params", {})
                ]
                if len(pairs) >= 3:
                    mi, mj = _mean([x[0] for x in pairs]), _mean([x[1] for x in pairs])
                    si, sj = max(_std([x[0] for x in pairs]), 1e-9), max(_std([x[1] for x in pairs]), 1e-9)
                    value = sum((a - mi) * (b - mj) for a, b in pairs) / max(len(pairs) - 1, 1) / si / sj
                    value = max(-0.75, min(0.75, value)) * 0.65
                else:
                    value = 0.0
                corr[i][j] = corr[j][i] = value
        try:
            lower = _cholesky(corr)
        except Exception:
            lower = _cholesky([[1.0 if i == j else 0.0 for j in range(len(numeric))] for i in range(len(numeric))])
        return numeric, means, stds, lower

    @staticmethod
    def _allowed_values(definition):
        raw = definition.get("allowed_values_json")
        if not raw:
            return []
        try:
            return json.loads(raw)
        except Exception:
            return []

    def _search_type(self, definition):
        explicit = str(definition.get("search_type") or "auto").strip().lower()
        if explicit not in ("", "auto"):
            return explicit
        value_type = str(definition.get("value_type") or "number").strip().lower()
        if value_type == "boolean":
            return "boolean"
        # IP grades are ordered integers even when a legacy DataMaster carried
        # only a few observed values. Treating that old list as the complete legal
        # domain prevents exploration of valid intermediate grades such as IP64.
        if value_type == "ip_grade":
            return "integer"
        allowed = self._allowed_values(definition)
        if allowed:
            numeric = [_float(value) for value in allowed]
            return "ordered_discrete" if all(value is not None for value in numeric) else "unordered_enum"
        if value_type in ("enum", "text"):
            return "unordered_enum"
        if int(definition.get("decimal_places") or 0) == 0:
            return "integer"
        return "continuous"

    def _normalized_allowed_values(self, definition):
        values = self._allowed_values(definition)
        kind = self._search_type(definition)
        if kind == "ordered_discrete":
            numeric = [_float(value) for value in values]
            if values and all(value is not None for value in numeric):
                return sorted(set(numeric))
        return list(values)

    def _value_equal(self, left, right, definition):
        kind = self._search_type(definition)
        if kind in ("boolean", "integer", "continuous", "ordered_discrete"):
            a, b = _float(left), _float(right)
            if a is None or b is None:
                return str(left) == str(right)
            tolerance = 10 ** (-max(2, int(definition.get("decimal_places", 3))))
            return abs(a - b) <= tolerance
        return str(left) == str(right)

    def _attribute_move_distance(self, current, candidate, definition):
        """Normalized one-attribute move size used to prefer minimal changes.

        When several neighbors already satisfy an output target, the generator
        should keep the closest legal value rather than jumping straight to an
        extreme bound. This makes paths such as IP65 -> IP64 discoverable and
        explainable.
        """
        kind = self._search_type(definition)
        if kind in ("boolean", "unordered_enum"):
            return 0.0 if self._value_equal(current, candidate, definition) else 1.0
        if kind == "ordered_discrete":
            allowed = self._normalized_allowed_values(definition)
            try:
                left = min(range(len(allowed)), key=lambda i: abs(float(allowed[i]) - float(current)))
                right = min(range(len(allowed)), key=lambda i: abs(float(allowed[i]) - float(candidate)))
                return abs(left - right) / max(len(allowed) - 1, 1)
            except Exception:
                return 0.0 if self._value_equal(current, candidate, definition) else 1.0
        current_num, candidate_num = _float(current), _float(candidate)
        if current_num is None or candidate_num is None:
            return 1.0
        lower = _float(definition.get("min_value"), current_num)
        upper = _float(definition.get("max_value"), current_num)
        return abs(candidate_num - current_num) / max(abs(upper - lower), 1e-9)

    def _attribute_neighbors(self, current, definition, std_value=0.0, step_scale=0.25, include_bounds=False, exhaustive_discrete=False):
        """Return legal one-attribute neighbors for a mixed search space."""
        kind = self._search_type(definition)
        values = []
        if kind == "boolean":
            return [0 if _truth(current) else 1]
        if kind in ("ordered_discrete", "unordered_enum"):
            allowed = self._normalized_allowed_values(definition)
            if not allowed:
                return []
            remaining = [value for value in allowed if not self._value_equal(value, current, definition)]
            if kind == "unordered_enum" or exhaustive_discrete or len(remaining) <= 10:
                return remaining
            current_num = _float(current)
            if current_num is None:
                return remaining[:10]
            remaining.sort(key=lambda value: abs(float(value) - current_num))
            edge = [allowed[0], allowed[-1]]
            return list(dict.fromkeys(remaining[:4] + edge))
        current_num = _float(current)
        if current_num is None:
            return []
        lower = _float(definition.get("min_value"), current_num)
        upper = _float(definition.get("max_value"), current_num)
        if lower > upper:
            lower, upper = upper, lower
        span = max(upper - lower, 1e-9)
        if kind == "integer":
            base_step = max(1, int(round(max(float(std_value or 0.0) * max(step_scale, 0.20), span * 0.035))))
            raw = [current_num - base_step, current_num + base_step, current_num - 3 * base_step, current_num + 3 * base_step]
            if include_bounds:
                raw.extend([lower, upper])
                if upper - lower <= 30:
                    raw.extend(range(int(math.ceil(lower)), int(math.floor(upper)) + 1))
            values = [int(round(_clamp(value, lower, upper))) for value in raw]
        else:
            step = max(float(std_value or 0.0) * max(step_scale, 0.20), span * 0.025, 10 ** (-max(1, int(definition.get("decimal_places", 3)))))
            raw = [current_num - step, current_num + step, current_num - 3.5 * step, current_num + 3.5 * step]
            if include_bounds:
                raw.extend([lower, upper, lower + span * 0.25, lower + span * 0.50, lower + span * 0.75])
            values = [_clamp(value, lower, upper) for value in raw]
        unique = []
        for value in values:
            if not self._value_equal(value, current, definition) and not any(self._value_equal(value, existing, definition) for existing in unique):
                unique.append(value)
        return unique

    def _anchor_demands(self, params, bounds, definitions):
        """Project user requirements first and return immutable anchor values.

        Engineering min/max remain hard bounds. If a request has no intersection
        with those bounds, the nearest engineering-bound value is used and the
        conflict is recorded; it will be shown as an unmet demand rather than
        silently failing generation.
        """
        locked = {}
        conflicts = []
        for key, rule in bounds.items():
            if key not in params or key not in definitions:
                continue
            definition = definitions[key]
            if not definition.get("auto_adjustable", 1):
                conflicts.append({"parameter_id": key, "reason": "该属性被设置为不允许自动调整"})
                continue
            engineering_min = float(definition.get("min_value") if definition.get("min_value") is not None else -1e99)
            engineering_max = float(definition.get("max_value") if definition.get("max_value") is not None else 1e99)
            allowed = rule.get("allowed")
            if allowed:
                numeric_allowed = [_float(x) for x in allowed]
                numeric_allowed = [x for x in numeric_allowed if x is not None and engineering_min <= x <= engineering_max]
                if numeric_allowed:
                    current = float(params[key])
                    value = min(numeric_allowed, key=lambda x: abs(x - current))
                else:
                    value = _clamp(float(params[key]), engineering_min, engineering_max)
                    conflicts.append({"parameter_id": key, "reason": "筛选允许值与工程范围没有交集"})
            else:
                requested_lo = float(rule.get("min", -1e99))
                requested_hi = float(rule.get("max", 1e99))
                lower = max(engineering_min, requested_lo)
                upper = min(engineering_max, requested_hi)
                current = float(params[key])
                if lower <= upper:
                    # Minimal-change projection: retain a satisfying seed value;
                    # otherwise move exactly to the nearest requested boundary.
                    if current < lower:
                        value = lower
                    elif current > upper:
                        value = upper
                    else:
                        value = current
                else:
                    target = requested_lo if requested_lo > engineering_max else requested_hi
                    value = _clamp(target, engineering_min, engineering_max)
                    conflicts.append({
                        "parameter_id": key,
                        "reason": "用户筛选范围与工程范围没有交集",
                        "requested_min": requested_lo,
                        "requested_max": requested_hi,
                    })
            kind = self._search_type(definition)
            if kind == "boolean":
                value = 1 if float(value) >= 0.5 else 0
            elif kind == "integer":
                value = int(round(float(value)))
            elif kind == "ordered_discrete":
                allowed_values = self._normalized_allowed_values(definition)
                if allowed_values:
                    value = min(allowed_values, key=lambda candidate: abs(float(candidate) - float(value)))
            locked[key] = value
            params[key] = value
        return locked, conflicts

    @staticmethod
    def _restore_locked(params, locked):
        for key, value in (locked or {}).items():
            params[key] = value
        return params

    def _contour_diagnostics(self, params, locked):
        details = []
        total = 0.0
        for model in self.runtime.effectiveness.couplings:
            target = model.get("target")
            if target not in params:
                continue
            try:
                band = self.runtime.effectiveness.coupling_band(model, params)
            except Exception:
                continue
            actual = float(params[target])
            width = max(float(band["upper"]) - float(band["lower"]), 1e-9)
            if actual < band["lower"]:
                absolute = float(band["lower"]) - actual
                ratio = absolute / width
                state = "below"
            elif actual > band["upper"]:
                absolute = actual - float(band["upper"])
                ratio = absolute / width
                state = "above"
            else:
                absolute = 0.0
                ratio = 0.0
                state = "inside"
            total += min(ratio, 4.0)
            label = self._parameter_definitions().get(target, {}).get("label", target)
            if state == "below":
                message = "%s低于模型经验区间下限%.3f，偏离%.3f" % (label, band["lower"], absolute)
            elif state == "above":
                message = "%s高于模型经验区间上限%.3f，偏离%.3f" % (label, band["upper"], absolute)
            else:
                message = "%s位于模型经验区间内" % label
            details.append({
                "target": target,
                "actual": round(actual, 6),
                "lower": round(float(band["lower"]), 6),
                "upper": round(float(band["upper"]), 6),
                "predicted": round(float(band["predicted"]), 6),
                "state": state,
                "deviation": round(absolute, 6),
                "deviation_ratio": round(ratio, 6),
                "target_locked": target in locked,
                "message": message,
            })
        return details, total / max(len(details), 1)

    def _evaluate_for_request(self, params, base_params, request):
        target_protocol = (request or {}).get("target_protocol")
        if target_protocol not in (None, ""):
            return self.evaluate_callback(params, base_params, target_protocol)
        try:
            return self.evaluate_callback(params, base_params)
        except TypeError:
            return self.evaluate_callback(params)

    def _repair_learned_boundaries(self, params, locked, definitions):
        repairs = []
        boundaries = getattr(self.runtime.effectiveness, "learned_boundaries", []) or []
        for item in boundaries:
            if not item.get("mature"):
                continue
            key = item.get("attribute_key") or item.get("parameter_id")
            if key not in params or key in locked or key not in definitions:
                continue
            value = _float(params.get(key))
            limit = _float(item.get("boundary"))
            if value is None or limit is None:
                continue
            side = item.get("side")
            violated = value < limit if side == "low" else value > limit if side == "high" else False
            if not violated:
                continue
            definition = definitions[key]
            span = max(
                float(definition.get("max_value") or 1) - float(definition.get("min_value") or 0),
                1e-9,
            )
            precision = int(definition.get("decimal_places") or 3)
            margin = max(10 ** (-precision), 0.001 * span)
            params[key] = limit + margin if side == "low" else limit - margin
            repairs.append("learned_boundary:%s:%s" % (key, side))
        return repairs

    def _soft_adjust_unlocked_contours(self, params, locked, definitions, strength=0.30):
        """Move only non-user targets toward empirical bands; never force inside."""
        moved = []
        for model in self.runtime.effectiveness.couplings:
            target = model.get("target")
            if target in locked or target not in params or not definitions.get(target, {}).get("auto_adjustable", 1):
                continue
            try:
                band = self.runtime.effectiveness.coupling_band(model, params)
            except Exception:
                continue
            actual = float(params[target])
            if actual < band["lower"]:
                destination = float(band["lower"])
            elif actual > band["upper"]:
                destination = float(band["upper"])
            else:
                # A weak centre pull reduces risk without collapsing diversity.
                destination = float(band["predicted"])
                strength_inside = min(strength, 0.12)
                params[target] = actual + strength_inside * (destination - actual)
                continue
            params[target] = actual + strength * (destination - actual)
            moved.append(target)
        return moved

    @staticmethod
    def _boundary_value(operator, rhs):
        return rhs + (1e-6 if operator == "gt" else -1e-6 if operator == "lt" else 0.0)

    def _repair_relations(self, params, seed, definitions, locked):
        """Repair explicit relations while preserving every user-anchored value."""
        repairs = []
        for item in self.store.coupling_rows():
            a, b = item.get("parameter_a"), item.get("parameter_b")
            if a not in params or b not in params or a not in seed or b not in seed or a not in definitions or b not in definitions:
                continue
            coupling_type = item.get("coupling_type")
            da, db = float(params[a]) - float(seed[a]), float(params[b]) - float(seed[b])
            if coupling_type in ("positive", "negative"):
                expected_sign = 1.0 if coupling_type == "positive" else -1.0
                mismatch = abs(da) > 1e-9 and (abs(db) <= 1e-9 or da * db * expected_sign < 0)
                if mismatch:
                    span_a = max(float(definitions[a].get("max_value") or 1) - float(definitions[a].get("min_value") or 0), 1e-9)
                    span_b = max(float(definitions[b].get("max_value") or 1) - float(definitions[b].get("min_value") or 0), 1e-9)
                    strength = max(float(item.get("strength") if item.get("strength") is not None else 0.35), 1e-6)
                    if b not in locked and definitions.get(b, {}).get("auto_adjustable", 1):
                        params[b] = float(seed[b]) + expected_sign * strength * (da / span_a) * span_b
                        repairs.append(item.get("coupling_id"))
                    elif a not in locked and definitions.get(a, {}).get("auto_adjustable", 1) and abs(db) > 1e-9:
                        params[a] = float(seed[a]) + expected_sign * (db / span_b) * span_a / strength
                        repairs.append(item.get("coupling_id"))
            if coupling_type == "feasible_domain":
                # Only relations explicitly marked severe are hard generation
                # constraints. Warning/info feasible domains remain visible risk
                # signals and must not erase a price- or demand-driven move.
                if str(item.get("severity") or "warning") != "error":
                    continue
                multiplier = float(item.get("multiplier") or 1)
                offset = float(item.get("offset") or 0)
                rhs = multiplier * float(params[a]) + offset
                operator = item.get("domain_operator") or "gte"
                if not self.store._compare(float(params[b]), operator, rhs):
                    if b not in locked and definitions.get(b, {}).get("auto_adjustable", 1):
                        params[b] = self._boundary_value(operator, rhs)
                        repairs.append(item.get("coupling_id"))
                    elif a not in locked and definitions.get(a, {}).get("auto_adjustable", 1) and abs(multiplier) > 1e-12:
                        params[a] = (float(params[b]) - offset) / multiplier
                        repairs.append(item.get("coupling_id"))
        for rule in self.store.constraint_rows():
            # Only severe rules are repaired as hard constraints. Advisory rules
            # are assessed and shown to the user but do not silently overwrite a
            # generated or manually edited attribute.
            if str(rule.get("severity") or "warning") != "error":
                continue
            left, right = rule.get("left_parameter"), rule.get("right_parameter")
            if left not in params or (right and right not in params):
                continue
            multiplier = float(rule.get("multiplier") or 1)
            offset = float(rule.get("offset") or 0)
            rhs = offset + (multiplier * float(params[right]) if right else 0)
            operator = rule.get("operator")
            if self.store._compare(float(params[left]), operator, rhs):
                continue
            if left not in locked and definitions.get(left, {}).get("auto_adjustable", 1):
                params[left] = self._boundary_value(operator, rhs)
                repairs.append(rule.get("rule_id"))
            elif right and right not in locked and definitions.get(right, {}).get("auto_adjustable", 1) and abs(multiplier) > 1e-12:
                params[right] = (float(params[left]) - offset) / multiplier
                repairs.append(rule.get("rule_id"))
        fitted_targets = set(
            item.get("target") for item in getattr(self.runtime.effectiveness, "couplings", []) or []
        )
        for edge in getattr(self.runtime.effectiveness, "coupling_edges", []) or []:
            source = edge.get("source")
            target = edge.get("target")
            if target in fitted_targets or source not in params or target not in params:
                continue
            if source not in seed or target not in seed or source not in definitions or target not in definitions:
                continue
            source_delta = float(params[source]) - float(seed[source])
            target_delta = float(params[target]) - float(seed[target])
            if abs(source_delta) <= 1e-9:
                continue
            expected_sign = 1.0 if edge.get("direction") == "positive" else -1.0
            mismatch = abs(target_delta) <= 1e-9 or source_delta * target_delta * expected_sign < 0
            if not mismatch or target in locked or not definitions[target].get("auto_adjustable", 1):
                continue
            prior = _float(edge.get("coefficient_prior"))
            if prior is not None:
                adjustment = expected_sign * abs(prior) * source_delta
            else:
                source_span = max(float(definitions[source].get("max_value") or 1) - float(definitions[source].get("min_value") or 0), 1e-9)
                target_span = max(float(definitions[target].get("max_value") or 1) - float(definitions[target].get("min_value") or 0), 1e-9)
                adjustment = expected_sign * 0.22 * (source_delta / source_span) * target_span
            params[target] = float(seed[target]) + adjustment
            repairs.append("direction_prior:%s->%s" % (source, target))
        self._restore_locked(params, locked)
        return repairs

    def _round_values(self, params, definitions, locked=None):
        for key, definition in definitions.items():
            if key not in params:
                continue
            kind = self._search_type(definition)
            allowed = self._normalized_allowed_values(definition)
            if kind in ("ordered_discrete", "unordered_enum") and allowed:
                if kind == "ordered_discrete":
                    current = _float(params[key])
                    if current is not None:
                        params[key] = min(allowed, key=lambda value: abs(float(value) - current))
                elif params[key] not in allowed:
                    params[key] = allowed[0]
            elif kind == "boolean":
                params[key] = 1 if _truth(params[key]) else 0
            elif kind in ("integer", "continuous"):
                value = _float(params[key])
                if value is None:
                    continue
                lower, upper = definition.get("min_value"), definition.get("max_value")
                if lower is not None and upper is not None:
                    value = _clamp(value, lower, upper)
                if kind == "integer":
                    params[key] = int(round(value))
                else:
                    params[key] = round(float(value), max(0, int(definition.get("decimal_places", 3))))
        self._restore_locked(params, locked or {})
        return params

    @staticmethod
    def _operator_text(operator):
        return {
            "gte": "不低于", "gt": "高于", "lte": "不高于", "lt": "低于", "eq": "等于",
            "boolean_is": "为", "text_equals": "等于", "text_contains": "包含",
            "range_inside": "位于区间", "range_contains": "覆盖区间", "range_overlap": "与区间相交",
        }.get(operator, operator)

    def _tag_branch_for_seed(self, request, seed_index):
        branches = self.store.tag_rule_branches(request.get("selected_tags"), max_branches=24)
        branch = branches[seed_index % max(len(branches), 1)] if branches else {"rules": [], "tag_groups": {}, "unresolved_tags": []}
        technical = []
        branch_request = dict(request)
        for rule in branch.get("rules") or []:
            key = str(rule.get("parameter_id") or "")
            op = rule.get("operator")
            try:
                value = float(str(rule.get("value1", "")).strip().upper().replace("IP", ""))
            except Exception:
                value = None
            if key == "__predicted_price_wan" and value is not None and op in ("lte", "lt", "eq"):
                current = branch_request.get("max_price")
                branch_request["max_price"] = value if current in (None, "") else min(float(current), value)
            elif key == "__capability_score" and value is not None and op in ("gte", "gt", "eq"):
                current = branch_request.get("min_capability")
                branch_request["min_capability"] = value if current in (None, "") else max(float(current), value)
            elif key == "__feasibility_probability" and value is not None and op in ("gte", "gt", "eq"):
                current = branch_request.get("min_feasibility")
                branch_request["min_feasibility"] = value if current in (None, "") else max(float(current), value)
            elif not key.startswith("__"):
                technical.append({"parameter_id": key, "operator": op, "value1": rule.get("value1"), "value2": rule.get("value2")})
        bounds = merge_bounds(
            filters_to_bounds(technical, "all"),
            filters_to_bounds(request.get("indicator_filters"), request.get("indicator_filter_mode", "all")),
        )
        return bounds, branch_request, {
            "tag_rule_groups": dict(branch.get("tag_groups") or {}),
            "unresolved_tags": list(branch.get("unresolved_tags") or []),
            "compiled_rule_count": len(branch.get("rules") or []),
        }

    @staticmethod
    def _output_target_penalty(evaluation, request):
        pieces = []
        checks = [
            ("max_price", effective_price(evaluation), "max", 4.0),
            ("min_capability", evaluation.get("capability_score"), "min", 2.0),
            ("min_cost_effectiveness", evaluation.get("cost_effectiveness"), "min", 1.5),
            ("min_feasibility", evaluation.get("feasibility_probability"), "min", 2.5),
        ]
        for key, actual, direction, weight in checks:
            target = request.get(key)
            if target in (None, "") or actual in (None, ""):
                continue
            target, actual = float(target), float(actual)
            gap = max(0.0, actual - target) if direction == "max" else max(0.0, target - actual)
            pieces.append(weight * gap / max(abs(target), 1.0))
        return sum(pieces)

    def _output_target_moves(self, center, base, locked, compensation, definitions, stds, step_scale, request, max_moves=12):
        """Build directional probes without evaluating either model.

        Older versions performed a complete coordinate-descent evaluation inside
        this method and then evaluated the returned candidates again in
        ``generate``.  With HTTP-backed price/effectiveness services that hidden
        nested search dominated generation time.  The adaptive beam already has
        the evaluated centre and promotes the best probes after each outer batch,
        so emit a compact, round-robin probe set and let the outer loop evaluate
        every unique candidate exactly once.
        """
        active = any(request.get(key) not in (None, "") for key in ("max_price", "min_capability", "min_cost_effectiveness", "min_feasibility"))
        if not active:
            return []
        normalized_center = dict(center)
        # Early rounds scan every adjustable attribute. Later rounds retain all
        # discrete attributes plus the highest-priority continuous attributes.
        full_scan = step_scale >= 0.27
        scan_keys = []
        for key in compensation:
            kind = self._search_type(definitions.get(key, {}))
            numeric_count = sum(
                1 for existing in scan_keys
                if self._search_type(definitions.get(existing, {})) in ("continuous", "integer")
            )
            if full_scan or kind in ("boolean", "ordered_discrete", "unordered_enum") or numeric_count < 6:
                scan_keys.append(key)

        exhaustive = request.get("max_price") not in (None, "")
        per_key = []
        for key in scan_keys:
            definition = definitions.get(key, {})
            if key in locked or key not in normalized_center or not definition.get("auto_adjustable", 1):
                continue
            neighbors = self._attribute_neighbors(
                normalized_center[key], definition, stds.get(key, 0.0), step_scale,
                include_bounds=True, exhaustive_discrete=exhaustive,
            )
            # Keep both local directions and engineering extremes.  Round-robin
            # emission below prevents an early high-cardinality field consuming
            # the entire outer evaluation budget.
            selected_values = []
            for value in neighbors[:4] + neighbors[-2:]:
                if not any(self._value_equal(value, existing, definition) for existing in selected_values):
                    selected_values.append(value)
            moves = []
            for value in selected_values:
                trial = dict(normalized_center)
                trial[key] = value
                moves.append((trial, "输出目标探针：%s调整为%s" % (definition.get("label", key), value)))
            if moves:
                per_key.append(moves)

        proposals = []
        depth = 0
        while len(proposals) < max_moves and any(depth < len(moves) for moves in per_key):
            for moves in per_key:
                if depth < len(moves):
                    proposals.append(moves[depth])
                    if len(proposals) >= max_moves:
                        break
            depth += 1
        unique = []
        signatures = set()
        for trial, label in proposals:
            signature = tuple((key, str(trial.get(key))) for key in sorted(trial))
            if signature not in signatures:
                signatures.add(signature)
                unique.append((trial, label))
        return unique[:max_moves]

    def _demand_assessment(self, item, request, definitions, tag_map):
        """Assess only user requirements; model extrapolation is reported separately."""
        unmet = []
        penalty = 0.0
        own_tags = set(item.get("tags") or [])
        for tag_id in request.get("selected_tags") or []:
            if tag_id not in own_tags:
                label = tag_map.get(tag_id, {}).get("tag_name", tag_id)
                unmet.append("缺少标签“%s”" % label)
                penalty += 2.0 * float(tag_map.get(tag_id, {}).get("weight", 1.0))
        params = item.get("params") or {}
        rules = request.get("indicator_filters") or []
        rule_results = [filter_match(params, rule) for rule in rules]
        if rules:
            if request.get("indicator_filter_mode", "all") == "any":
                failed_rules = [] if any(rule_results) else rules
            else:
                failed_rules = [rule for rule, matched in zip(rules, rule_results) if not matched]
            for rule in failed_rules:
                key = rule.get("parameter_id")
                definition = definitions.get(key, {})
                label = definition.get("label", key)
                operator = self._operator_text(rule.get("operator"))
                value1, value2 = rule.get("value1"), rule.get("value2")
                expected = "%s～%s" % (value1, value2) if rule.get("operator", "").startswith("range_") else str(value1)
                actual = params.get(key)
                unmet.append("%s当前为%s，未达到“%s %s”" % (label, actual, operator, expected))
                span = max(float(definition.get("max_value") or 1) - float(definition.get("min_value") or 0), 1e-9)
                a, b = _float(actual), _float(value1)
                penalty += 1.0 + (abs(a - b) / span if a is not None and b is not None else 0.5)
        evaluation = item.get("evaluation") or {}
        output_checks = [
            ("max_price", "预测价格", effective_price(evaluation), "max", "万元"),
            ("min_capability", "保守效能分", evaluation.get("conservative_capability_score", evaluation.get("capability_score")), "min", ""),
            ("min_cost_effectiveness", "效费比", evaluation.get("cost_effectiveness"), "min", ""),
            ("min_feasibility", "可行概率", evaluation.get("feasibility_probability"), "min", ""),
        ]
        for key, label, actual, direction, unit in output_checks:
            target = request.get(key)
            if target in (None, "") or actual in (None, ""):
                continue
            actual, target = float(actual), float(target)
            violated = actual > target if direction == "max" else actual < target
            if violated:
                relation = "高于上限" if direction == "max" else "低于要求"
                unmet.append("%s%s：当前%.3f%s，要求%.3f%s" % (label, relation, actual, unit, target, unit))
                penalty += 1.5 + abs(actual - target) / max(abs(target), 1.0)
        unique = []
        for text in unmet:
            if text not in unique:
                unique.append(text)
        return unique, round(penalty, 6)

    @staticmethod
    def _engineering_conflicts(evaluation):
        messages = []
        penalty = 0.0
        for message in evaluation.get("rule_messages") or []:
            # Anomaly is empirical/model-domain risk, not a hard engineering rule.
            if message.get("severity") == "error" and message.get("source") != "anomaly":
                text = "工程规则：%s" % (message.get("message") or message.get("title") or "存在阻断风险")
                if text not in messages:
                    messages.append(text)
                    penalty += 1.0
        gate = evaluation.get("physical_gate") or {}
        if gate.get("passed") is False:
            labels = {
                "reject_hard_violation": "超出硬生成范围",
                "reject_mature_expert_boundary": "命中成熟专家不可行边界",
                "reject_severe_coupling": "存在严重耦合不匹配",
                "reject_low_feasibility_probability": "可行概率低于工程准入阈值",
            }
            text = "物理门控：%s" % labels.get(gate.get("decision"), gate.get("decision") or "未通过")
            if text not in messages:
                messages.append(text)
                penalty += 4.0
        return messages, penalty

    def _extrapolation_assessment(self, evaluation, contour_details):
        warnings = []
        contour_penalty = 0.0
        for detail in contour_details:
            if detail.get("state") == "inside":
                continue
            contour_penalty += min(float(detail.get("deviation_ratio") or 0), 4.0)
            prefix = "已保留用户筛选值；" if detail.get("target_locked") else ""
            warnings.append(prefix + detail.get("message", "超出模型经验区间"))
        anomaly = evaluation.get("anomaly_assessment") or {}
        if anomaly.get("status") in ("caution", "out_of_domain"):
            warnings.append(anomaly.get("message") or "部分属性超出模型主要训练范围")
        unique = []
        for text in warnings:
            if text and text not in unique:
                unique.append(text)
        return unique, contour_penalty / max(len(contour_details), 1), float(anomaly.get("score") or 0.0)

    def _objective_value(self, evaluation):
        capability = float(evaluation.get("conservative_capability_score", evaluation.get("capability_score")) or 0) / 100.0
        feasibility = float(evaluation.get("feasibility_probability") or 0)
        ce = float(evaluation.get("cost_effectiveness") or 0)
        price = float(effective_price(evaluation) or 0)
        price_utility = 1.0 / (1.0 + max(price, 0.0) / 20.0)
        ce_utility = ce / (1.0 + abs(ce))
        gate_penalty = 1.5 if (evaluation.get("physical_gate") or {}).get("passed") is False else 0.0
        return 0.38 * capability + 0.27 * feasibility + 0.18 * ce_utility + 0.17 * price_utility - gate_penalty

    def _record_from_params(self, params, base, request, definitions, tag_map, locked, anchor_conflicts,
                            repairs, soft_moves, iteration, attempt, search_move, evaluation=None):
        if evaluation is None:
            evaluation = self._evaluate_for_request(params, base.get("params"), request)
        contour_details, _ = self._contour_diagnostics(evaluation.get("parameters") or params, locked)
        tags = self.store.derive_tags(params, evaluation, base.get("tags"))
        tag_evidence = self.store.tag_evidence(params, evaluation, base.get("tags"))
        item = {
            "agreement_id": "",
            "product_code": self.runtime.schema["product_code"],
            "agreement_name": "",
            "positioning": self.store._positioning(tags),
            "agreement_source": "live_generated",
            "source_year": time.localtime().tm_year,
            "supplier_type": "智能目标邻域生成",
            "historical_price_wan": None,
            "predicted_price_wan": evaluation["predicted_price_wan"],
            'recommendation_price_wan':evaluation.get('recommendation_price_wan',evaluation['predicted_price_wan']),
            'price_risk':evaluation.get('price_risk'),
            "price_interval_wan": evaluation["price_interval_wan"],
            "capability_score": evaluation["capability_score"],
            "conservative_capability_score": evaluation.get("conservative_capability_score", evaluation["capability_score"]),
            "protocol_score_interval": evaluation.get("protocol_score_interval"),
            "support_at_80": evaluation.get("support_at_80"),
            "support_at_100": evaluation.get("support_at_100"),
            "score_uncertainty_width": evaluation.get("score_uncertainty_width"),
            "feasibility_probability": evaluation["feasibility_probability"],
            "physical_gate": evaluation.get("physical_gate") or {},
            "cost_effectiveness": evaluation["cost_effectiveness"],
            "params": dict(params),
            "tags": tags,
            "tag_evidence": tag_evidence,
            "is_generated": True,
            "evaluation": evaluation,
        }
        demand_unmet, demand_penalty = self._demand_assessment(item, request, definitions, tag_map)
        hard_conflicts, hard_penalty = self._engineering_conflicts(evaluation)
        extrapolation, contour_penalty, anomaly_penalty = self._extrapolation_assessment(evaluation, contour_details)
        seed_distance = self._normalized_distance(params, base["params"], definitions)
        objective = self._objective_value(evaluation)
        item.update({
            "unmet_conditions": demand_unmet + hard_conflicts,
            "demand_unmet_conditions": demand_unmet,
            "engineering_conflicts": hard_conflicts,
            "extrapolation_warnings": extrapolation,
            "contour_extrapolation": any(x.get("state") != "inside" for x in contour_details),
            "fit_penalty": round(demand_penalty + 2.5 * hard_penalty, 6),
            "strict_filter_satisfied": not demand_unmet and not hard_conflicts,
            "best_effort": bool(demand_unmet or hard_conflicts),
            "search_metrics": {
                "hard_penalty": hard_penalty,
                "demand_penalty": demand_penalty,
                "contour_penalty": round(contour_penalty, 6),
                "anomaly_penalty": round(anomaly_penalty, 6),
                "objective_value": round(objective, 6),
                "seed_distance": round(seed_distance, 6),
            },
            "generation_trace": {
                "method": "batched_directional_beam_search",
                "seed_agreement_id": base["agreement_id"],
                "locked_parameters": sorted(locked),
                "anchor_conflicts": list(anchor_conflicts or []),
                "explicit_repairs": [x for x in repairs if x],
                "soft_contour_moves": sorted(set(soft_moves or [])),
                "contour_assessments": contour_details,
                "search_move": search_move,
                "active_output_targets": dict((key, request.get(key)) for key in ("max_price", "min_capability", "min_cost_effectiveness", "min_feasibility") if request.get(key) not in (None, "")),
                "request_context": {
                    "selected_tags": list(request.get("selected_tags") or []),
                    "indicator_filter_mode": request.get("indicator_filter_mode", "all"),
                    "indicator_filters": list(request.get("indicator_filters") or []),
                    "max_price": request.get("max_price"),
                    "min_capability": request.get("min_capability"),
                    "min_cost_effectiveness": request.get("min_cost_effectiveness"),
                    "min_feasibility": request.get("min_feasibility"),
                    "target_protocol": request.get("target_protocol"),
                },
                "seed_tags": list(base.get("tags") or []),
                "iteration": iteration,
                "attempt": attempt,
            },
        })
        gate = evaluation.get("physical_gate") or {}
        risk_signature = []
        if gate.get("passed") is False:
            risk_signature.append(str(gate.get("decision") or "physical_gate_rejected"))
            for boundary in gate.get("mature_boundary_violations") or []:
                risk_signature.append("boundary:%s:%s" % (
                    boundary.get("attribute_key") or boundary.get("parameter_id"),
                    boundary.get("side"),
                ))
            for mismatch in gate.get("severe_coupling_mismatches") or []:
                risk_signature.append("coupling:%s:%s" % (
                    mismatch.get("target_key") or mismatch.get("target"),
                    mismatch.get("status") or mismatch.get("state"),
                ))
        item["_risk_signature"] = tuple(sorted(set(risk_signature)))
        item["generation_trace"]["risk_signature"] = list(item["_risk_signature"])
        if item["best_effort"] or hard_conflicts or anomaly_penalty >= 0.18 or contour_penalty >= 0.70:
            confidence = "low"
        elif extrapolation or anomaly_penalty > 0:
            confidence = "medium"
        else:
            confidence = "high"
        item["recommendation_confidence"] = confidence
        if item["best_effort"]:
            item["recommendation_warning"] = "该方案未完全满足全部需求或工程硬规则：%s" % "；".join(item["unmet_conditions"][:3])
        elif extrapolation:
            item["recommendation_warning"] = "该方案满足用户条件，但部分属性位于历史经验或模型训练范围之外：%s" % "；".join(extrapolation[:3])
        else:
            item["recommendation_warning"] = ""
        # Lexicographic comparison follows the agreed priority order.
        item["_search_key"] = (
            round(hard_penalty, 8),
            round(demand_penalty, 8),
            round(contour_penalty, 8),
            round(anomaly_penalty, 8),
            round(-objective, 8),
            round(seed_distance, 8),
        )
        return item

    def _compensation_keys(self, locked, definitions, numeric):
        priority = []
        for model in self.runtime.effectiveness.couplings:
            if model.get("target") in locked:
                for source in model.get("sources", []):
                    key = source.get("key")
                    if key and key not in locked and definitions.get(key, {}).get("auto_adjustable", 1) and key not in priority:
                        priority.append(key)
        for relation in self.store.coupling_rows():
            a, b = relation.get("parameter_a"), relation.get("parameter_b")
            if a in locked and b not in locked and definitions.get(b, {}).get("auto_adjustable", 1) and b not in priority:
                priority.append(b)
            if b in locked and a not in locked and definitions.get(a, {}).get("auto_adjustable", 1) and a not in priority:
                priority.append(a)
        for key in numeric:
            if key not in locked and key not in priority:
                priority.append(key)
        # Every legal adjustable discrete, enum and boolean attribute participates
        # in target search; display order no longer prevents late attributes such
        # as protection grade from being explored.
        for key, definition in definitions.items():
            if key not in locked and definition.get("auto_adjustable", 1) and key not in priority:
                priority.append(key)
        return priority

    def _reverse_contour_moves(self, center, base, locked, compensation, definitions, stds, step_scale, max_moves=6):
        """Finite-difference moves that reduce deviation of locked contour targets."""
        current_details, current_penalty = self._contour_diagnostics(center, locked)
        locked_outside = [x for x in current_details if x.get("target_locked") and x.get("state") != "inside"]
        if not locked_outside:
            return []
        source_keys = []
        targets = set(x["target"] for x in locked_outside)
        for model in self.runtime.effectiveness.couplings:
            if model.get("target") in targets:
                for source in model.get("sources", []):
                    key = source.get("key")
                    if key in compensation and key not in source_keys:
                        source_keys.append(key)
        proposals = []
        for key in source_keys[:8]:
            definition = definitions.get(key, {})
            if definition.get("value_type") == "boolean":
                trial = dict(center)
                trial[key] = 1 - int(float(trial[key]))
                self._restore_locked(trial, locked)
                _details, penalty = self._contour_diagnostics(trial, locked)
                proposals.append((penalty - current_penalty, trial, "反向轮廓：切换%s" % definition.get("label", key)))
                continue
            step = max(float(stds.get(key, 0.0)) * step_scale, (
                float(definition.get("max_value") or 1) - float(definition.get("min_value") or 0)
            ) * 0.01)
            best = None
            for sign in (-1.0, 1.0):
                trial = dict(center)
                trial[key] = float(trial[key]) + sign * step
                self._round_values(trial, definitions, locked)
                _details, penalty = self._contour_diagnostics(trial, locked)
                candidate = (penalty - current_penalty, trial, "反向轮廓：%s%s" % (definition.get("label", key), "上调" if sign > 0 else "下调"))
                if best is None or candidate[0] < best[0]:
                    best = candidate
            if best is not None:
                proposals.append(best)
        proposals.sort(key=lambda x: x[0])
        return [(trial, label) for improvement, trial, label in proposals[:max_moves] if improvement < -1e-9]

    def _neighborhood_params(self, center, base, locked, compensation, definitions, numeric, stds, lower,
                             step_scale, rng, iteration, request):
        proposals = []
        proposals.extend(self._output_target_moves(center, base, locked, compensation, definitions, stds, step_scale, request))
        proposals.extend(self._reverse_contour_moves(center, base, locked, compensation, definitions, stds, step_scale))

        # One-dimensional probes use the legal neighborhood of each attribute.
        # All discrete/enum/boolean fields are always included; continuous fields
        # retain a compact priority subset for performance.
        probe_keys = []
        for key in compensation:
            kind = self._search_type(definitions[key])
            if kind in ("boolean", "ordered_discrete", "unordered_enum") or len([x for x in probe_keys if self._search_type(definitions[x]) in ("continuous", "integer")]) < 5:
                probe_keys.append(key)
        for key in probe_keys:
            definition = definitions[key]
            neighbors = self._attribute_neighbors(center.get(key), definition, stds.get(key, 0.0), step_scale, include_bounds=False, exhaustive_discrete=False)
            for value in neighbors[:6]:
                trial = dict(center)
                trial[key] = value
                proposals.append((trial, "单属性：%s调整为%s" % (definition.get("label", key), value)))

        # Two-variable coordinated moves support compensation trade-offs.
        numeric_comp = [key for key in compensation if key in stds][:5]
        for index in range(min(3, max(len(numeric_comp) - 1, 0))):
            a, b = numeric_comp[index], numeric_comp[index + 1]
            trial = dict(center)
            sign_a = -1.0 if (iteration + index) % 2 else 1.0
            sign_b = -sign_a if index % 2 else sign_a
            trial[a] = float(trial[a]) + sign_a * float(stds[a]) * step_scale
            trial[b] = float(trial[b]) + sign_b * float(stds[b]) * step_scale
            proposals.append((trial, "双属性联动：%s/%s" % (definitions[a].get("label", a), definitions[b].get("label", b))))

        # Correlated stochastic moves preserve local joint structure while the
        # iterative beam update makes subsequent rounds directional.
        for sample_index in range(3):
            trial = dict(center)
            noise = _mat_vec(lower, [rng.gauss(0, 1) for _ in numeric]) if numeric else []
            for idx, key in enumerate(numeric):
                if key in locked or not definitions.get(key, {}).get("auto_adjustable", 1):
                    continue
                trial[key] = float(trial[key]) + step_scale * float(stds[key]) * noise[idx]
            proposals.append((trial, "相关联合扰动-%d" % (sample_index + 1)))
        return proposals

    def _deep_extrapolation_moves(self, beam, definitions, max_moves=20):
        """Recombine useful changes from elite parents for mixed-domain depth."""
        if len(beam) < 2:
            return []
        elites = sorted(beam, key=lambda item: item["_search_key"])[:6]
        proposals = []
        for i in range(len(elites)):
            for j in range(i + 1, len(elites)):
                left_record, right_record = elites[i], elites[j]
                left, right = left_record["params"], right_record["params"]
                locked = left_record.get("_locked") or {}
                child = dict(left)
                changed = []
                for key, definition in definitions.items():
                    if key in locked or key not in left or key not in right or not definition.get("auto_adjustable", 1):
                        continue
                    if self._value_equal(left[key], right[key], definition):
                        continue
                    kind = self._search_type(definition)
                    if kind in ("continuous", "integer"):
                        a, b = _float(left[key]), _float(right[key])
                        if a is None or b is None:
                            continue
                        child[key] = a + 0.35 * (a - b) if (i + j + len(changed)) % 2 else (a + b) / 2.0
                    else:
                        child[key] = right[key]
                    changed.append(key)
                    if len(changed) >= 3:
                        break
                if changed:
                    self._round_values(child, definitions, locked)
                    proposals.append((child, left_record, "深度精英交叉：%s" % "/".join(changed)))
                if len(proposals) >= max_moves:
                    return proposals
        return proposals

    def _finalize_params(self, params, base, locked, definitions, soft_strength=0.30):
        self._restore_locked(params, locked)
        boundary_repairs = self._repair_learned_boundaries(params, locked, definitions)
        soft_moves = self._soft_adjust_unlocked_contours(params, locked, definitions, strength=soft_strength)
        repairs = self._repair_relations(params, base["params"], definitions, locked)
        repairs.extend(self._repair_learned_boundaries(params, locked, definitions))
        repairs.extend(boundary_repairs)
        repairs = list(dict.fromkeys(value for value in repairs if value))
        self._restore_locked(params, locked)
        self._round_values(params, definitions, locked)
        return repairs, soft_moves

    def _changed_parameters(self, params, seed, definitions):
        changed = []
        for key, definition in definitions.items():
            if key not in params or key not in seed:
                continue
            if not self._value_equal(params[key], seed[key], definition):
                changed.append(key)
        return changed

    def _beam_select(self, records, definitions, width=10):
        records = sorted(records, key=lambda x: x["_search_key"])
        selected = []
        for item in records:
            if not selected or min(self._normalized_distance(item["params"], x["params"], definitions) for x in selected) >= 0.008:
                selected.append(item)
            if len(selected) >= width:
                break
        if len(selected) < width:
            for item in records:
                if item not in selected:
                    selected.append(item)
                if len(selected) >= width:
                    break
        return selected

    def _diverse_strict_count(self, records, definitions, historical=None, distance=0.018):
        """Count strict solutions that can actually survive final diversity selection."""
        selected = []
        for item in sorted(
            (record for record in records if record.get("strict_filter_satisfied")),
            key=lambda record: record["_search_key"],
        ):
            if historical and any(
                self._normalized_distance(item["params"], source["params"], definitions) < 0.002
                for source in historical
            ):
                continue
            if not selected or min(
                self._normalized_distance(item["params"], existing["params"], definitions)
                for existing in selected
            ) >= distance:
                selected.append(item)
        return len(selected)

    def _make_public_item(self, record, base, changed, index, rng):
        item = dict(record)
        for private_key in ("_search_key", "_base", "_locked", "_anchor_conflicts", "_changed", "_request", "_risk_signature"):
            item.pop(private_key, None)
        trace = dict(item.get("generation_trace") or {})
        trace["changed_parameters"] = list(changed)
        item["generation_trace"] = trace
        item["agreement_id"] = "LIVE-%06d-%03d" % (rng.randint(0, 999999), index)
        item["agreement_name"] = "基于%s生成的候选协议-%02d" % (base["agreement_id"], index)
        item["generation_note"] = "根据用户条件、标签规则和模型输出目标进行迭代优化，并完成价格与效能联合评价。"
        if item.get("best_effort"):
            item["solution_fit_label"] = "探索方案"
            item["solution_fit_summary"] = "仍有需求或工程硬规则未满足，请结合风险提示继续调整"
        elif item.get("extrapolation_warnings"):
            item["solution_fit_label"] = "需求满足·外推评估"
            item["solution_fit_summary"] = "已满足筛选条件，但部分组合超出历史经验范围，预测可信度下降"
        else:
            item["solution_fit_label"] = "满足条件"
            item["solution_fit_summary"] = "已满足当前筛选条件且位于主要模型经验范围内"
        return item

    def _emergency_candidate(self, seeds, bounds, definitions, request, tag_map, rng):
        for base in seeds:
            params = dict(base.get("params") or {})
            locked, conflicts = self._anchor_demands(params, bounds, definitions)
            changed = self._changed_parameters(params, base["params"], definitions)
            for key, definition in definitions.items():
                if key in locked or key not in params or not definition.get("auto_adjustable", 1):
                    continue
                neighbors = self._attribute_neighbors(params[key], definition, 0.0, 0.30, include_bounds=True, exhaustive_discrete=True)
                if not neighbors:
                    continue
                params[key] = neighbors[0]
                if key not in changed:
                    changed.append(key)
                if len(changed) >= 2:
                    break
            repairs, soft = self._finalize_params(params, base, locked, definitions)
            try:
                record = self._record_from_params(params, base, request, definitions, tag_map, locked, conflicts, repairs, soft, 0, 0, "兜底微调")
            except Exception:
                continue
            return record, base
        return None, None

    def generate(self, request, count=10, seed=None, budget=1200, search_mode="fast", progress_callback=None):
        rng = random.Random(seed if seed is not None else int(time.time() * 1000) % 2147483647)
        deep_search = str(search_mode or "fast") == "deep_extrapolation"
        definitions = self._parameter_definitions()
        target_protocol = request.get("target_protocol")
        try:
            history = self.store.historical_agreements(target_protocol=target_protocol)
        except TypeError:
            history = self.store.historical_agreements()
        seeds = self.select_seeds(request, max(8, min(16, len(history))), historical=history)
        if not seeds:
            raise ValueError("没有历史协议可作为生成种子")
        numeric, _means, stds, lower = self._local_statistics(seeds, definitions)
        tag_map = self.store.tag_map()
        branch_bounds = []
        tag_weights = dict((key, value.get("weight", 1.0)) for key, value in tag_map.items())
        all_records = []
        seen = set()
        rejection = {"not_changed": 0, "duplicate": 0, "model_input": 0, "hard_conflict": 0, "demand_unmet": 0, "extrapolation": 0, "known_boundary_repaired": 0, "repeated_risk_signature": 0}
        evaluations = 0
        max_evaluations = max(int(budget), count * 10)
        beam = []

        # Stage 1: demand-anchored centres from every selected historical seed.
        # They are evaluated together.  Previous releases issued one price and one
        # effectiveness HTTP request per seed before the actual search started.
        initial_pending = []
        for seed_index, base in enumerate(seeds):
            bounds, branch_request, branch_info = self._tag_branch_for_seed(request, seed_index)
            branch_bounds.append(bounds)
            params = dict(base["params"])
            locked, anchor_conflicts = self._anchor_demands(params, bounds, definitions)
            repairs, soft_moves = self._finalize_params(params, base, locked, definitions, soft_strength=0.18)
            signature = tuple((key, params[key]) for key in sorted(params))
            if signature in seen:
                continue
            seen.add(signature)
            initial_pending.append({
                "params": params,
                "base": base,
                "request": branch_request,
                "locked": locked,
                "anchor_conflicts": anchor_conflicts,
                "repairs": repairs,
                "soft_moves": soft_moves,
                "branch_info": branch_info,
                "attempt": seed_index + 1,
            })

        initial_evaluations = None
        if initial_pending and self.evaluate_batch_callback:
            try:
                initial_evaluations = self.evaluate_batch_callback([
                    {
                        "candidate_id": "SEED-%04d" % index,
                        "parameters": item["params"],
                        "base_parameters": item["base"].get("params"),
                        "target_protocol": item["request"].get("target_protocol"),
                    }
                    for index, item in enumerate(initial_pending)
                ])
                if len(initial_evaluations) != len(initial_pending):
                    initial_evaluations = None
            except Exception:
                initial_evaluations = None

        for initial_index, item in enumerate(initial_pending):
            try:
                evaluation = initial_evaluations[initial_index] if initial_evaluations is not None else None
                record = self._record_from_params(
                    item["params"], item["base"], item["request"], definitions, tag_map,
                    item["locked"], item["anchor_conflicts"], item["repairs"], item["soft_moves"],
                    0, item["attempt"], "需求与标签条件投影", evaluation=evaluation,
                )
            except Exception:
                rejection["model_input"] += 1
                continue
            evaluations += 1
            record["_base"] = item["base"]
            record["_locked"] = item["locked"]
            record["_anchor_conflicts"] = item["anchor_conflicts"]
            record["_request"] = item["request"]
            record["generation_trace"]["tag_branch"] = item["branch_info"]
            all_records.append(record)
            beam.append(record)
        beam_width = 14 if deep_search else 10
        beam = self._beam_select(beam, definitions, width=min(beam_width, max(4, len(beam))))

        # Stage 2: adaptive iterative neighborhoods. Better candidates become the
        # next centres; the step size shrinks as the search progresses.
        step_schedule = (
            [0.52, 0.90, 0.68, 0.50, 0.34, 0.22, 0.13]
            if deep_search else [0.52, 0.38, 0.27, 0.19, 0.13, 0.09]
        )
        iteration = 0
        while beam and evaluations < max_evaluations and iteration < len(step_schedule):
            iteration += 1
            step_scale = step_schedule[iteration - 1]
            round_records = list(beam)
            pending = []
            remaining_rounds = len(step_schedule) - iteration + 1
            remaining_budget = max_evaluations - evaluations
            # Deep extrapolation must retain budget for cumulative multi-attribute
            # movement.  A single wide first-round coordinate scan previously
            # consumed the whole budget and could never combine useful moves.
            round_budget = (
                (
                    min(120, remaining_budget)
                    if iteration == 1 else
                    max(24, int(math.ceil(float(remaining_budget) / remaining_rounds)))
                )
                if deep_search else remaining_budget
            )
            round_budget = min(round_budget, remaining_budget)
            proposals_by_center = []
            proposed_signatures = set()
            for center_record in beam:
                base = center_record["_base"]
                locked = center_record["_locked"]
                anchor_conflicts = center_record["_anchor_conflicts"]
                center_request = center_record.get("_request") or request
                compensation = self._compensation_keys(locked, definitions, numeric)
                proposals = self._neighborhood_params(
                    center_record["params"], base, locked, compensation, definitions,
                    numeric, stds, lower, step_scale, rng, iteration, center_request,
                )
                center_pending = []
                for proposal_index, (params, move_label) in enumerate(proposals):
                    repairs, soft_moves = self._finalize_params(params, base, locked, definitions, soft_strength=0.32)
                    changed = self._changed_parameters(params, base["params"], definitions)
                    if len(changed) < 1:
                        rejection["not_changed"] += 1
                        continue
                    signature = tuple((key, params[key]) for key in sorted(params))
                    if signature in seen or signature in proposed_signatures:
                        rejection["duplicate"] += 1
                        continue
                    proposed_signatures.add(signature)
                    center_pending.append({
                        "params": params, "base": base, "request": center_request,
                        "locked": locked, "anchor_conflicts": anchor_conflicts,
                        "repairs": repairs, "soft_moves": soft_moves,
                        "move_label": move_label, "center_record": center_record,
                        "signature": signature,
                    })
                if center_pending:
                    proposals_by_center.append(center_pending)
            if deep_search and iteration >= 2:
                for params, center_record, move_label in self._deep_extrapolation_moves(beam, definitions):
                    base = center_record["_base"]
                    locked = center_record["_locked"]
                    repairs, soft_moves = self._finalize_params(
                        params, base, locked, definitions, soft_strength=0.24,
                    )
                    signature = tuple((key, params[key]) for key in sorted(params))
                    if signature in seen or signature in proposed_signatures:
                        continue
                    proposed_signatures.add(signature)
                    proposals_by_center.insert(0, [{
                        "params": params, "base": base,
                        "request": center_record.get("_request") or request,
                        "locked": locked,
                        "anchor_conflicts": center_record["_anchor_conflicts"],
                        "repairs": repairs, "soft_moves": soft_moves,
                        "move_label": move_label, "center_record": center_record,
                        "signature": signature,
                    }])
            if deep_search and iteration > 1:
                # Round-robin allocation gives every promising centre a chance.
                proposal_depth = 0
                while len(pending) < round_budget and any(
                    proposal_depth < len(items) for items in proposals_by_center
                ):
                    for items in proposals_by_center:
                        if proposal_depth < len(items):
                            pending.append(items[proposal_depth])
                            if len(pending) >= round_budget:
                                break
                    proposal_depth += 1
            else:
                # The first deep round deliberately uses the exact fast-path
                # ordering.  Deep exploration therefore retains every good
                # shallow result before spending later budgets on combinations.
                for items in proposals_by_center:
                    for item in items:
                        pending.append(item)
                        if len(pending) >= round_budget:
                            break
                    if len(pending) >= round_budget:
                        break
            for item in pending:
                seen.add(item["signature"])
            if progress_callback:
                base_progress = 20 if deep_search else 35
                span = 62 if deep_search else 48
                progress_callback(
                    min(84, base_progress + int(span * iteration / max(len(step_schedule), 1))),
                    (
                        "筛选条件越界较多：正在进行第%d/%d轮多属性组合探索；外推预测仅供参考"
                        % (iteration, len(step_schedule))
                        if deep_search else "正在评价第%d轮候选方案" % iteration
                    ),
                )
            batch_evaluations = None
            if pending and self.evaluate_batch_callback:
                try:
                    batch_evaluations = self.evaluate_batch_callback([
                        {
                            "candidate_id": "SEARCH-%02d-%04d" % (iteration, index),
                            "parameters": item["params"],
                            "base_parameters": item["base"].get("params"),
                            "target_protocol": item["request"].get("target_protocol"),
                        }
                        for index, item in enumerate(pending)
                    ])
                    if len(batch_evaluations) != len(pending):
                        batch_evaluations = None
                except Exception:
                    # Keep the old candidate-level failure semantics if a remote
                    # batch cannot be processed as a whole.
                    batch_evaluations = None
            for pending_index, item in enumerate(pending):
                try:
                    evaluation = batch_evaluations[pending_index] if batch_evaluations is not None else None
                    record = self._record_from_params(
                        item["params"], item["base"], item["request"], definitions, tag_map,
                        item["locked"], item["anchor_conflicts"], item["repairs"], item["soft_moves"],
                        iteration, evaluations + 1, item["move_label"], evaluation=evaluation,
                    )
                except Exception:
                    rejection["model_input"] += 1
                    continue
                evaluations += 1
                record["_base"] = item["base"]
                record["_locked"] = item["locked"]
                record["_anchor_conflicts"] = item["anchor_conflicts"]
                record["_request"] = item["request"]
                record["generation_trace"]["tag_branch"] = dict(item["center_record"].get("generation_trace", {}).get("tag_branch") or {})
                round_records.append(record)
                all_records.append(record)
            beam = self._beam_select(round_records, definitions, width=beam_width)
            # Stop as soon as final selection can return the requested number of
            # genuinely different strict solutions.  Counting three times the
            # requested amount caused unnecessary model batches on easy demands.
            diverse_strict = self._diverse_strict_count(all_records, definitions, historical=history)
            if diverse_strict >= count:
                break
            if not pending:
                break

        # Remove candidates that are effectively unchanged from every historical
        # protocol; user anchoring can still produce one-attribute changes, which
        # are valid when that attribute is precisely the requested extrapolation.
        usable = []
        for record in all_records:
            if any(self._normalized_distance(record["params"], historical["params"], definitions) < 0.002 for historical in history):
                rejection["duplicate"] += 1
                continue
            changed = self._changed_parameters(record["params"], record["_base"]["params"], definitions)
            if not changed:
                rejection["not_changed"] += 1
                continue
            record["_changed"] = changed
            if record.get("engineering_conflicts"):
                rejection["hard_conflict"] += 1
            if record.get("demand_unmet_conditions"):
                rejection["demand_unmet"] += 1
            if record.get("extrapolation_warnings"):
                rejection["extrapolation"] += 1
            rejection["known_boundary_repaired"] += sum(
                1 for value in record.get("generation_trace", {}).get("explicit_repairs", [])
                if str(value).startswith("learned_boundary:")
            )
            usable.append(record)

        if not usable:
            emergency, emergency_base = self._emergency_candidate(seeds, bounds, definitions, request, tag_map, rng)
            if emergency is not None:
                emergency["_base"] = emergency_base
                emergency["_changed"] = self._changed_parameters(emergency["params"], emergency_base["params"], definitions)
                usable.append(emergency)

        strict_candidates = [item for item in usable if item.get("strict_filter_satisfied")]
        strict_available = bool(strict_candidates)
        if strict_available:
            ranking_pool = []
            for item in strict_candidates:
                public = dict(item)
                for key in ("_base", "_locked", "_anchor_conflicts", "_changed", "_search_key", "_request", "_risk_signature"):
                    public.pop(key, None)
                ranking_pool.append(public)
            ranked_public = rank_agreements(ranking_pool, request, tag_weights)
            by_signature = dict((tuple((k, x["params"][k]) for k in sorted(x["params"])), x) for x in strict_candidates)
            ranked = []
            for ranked_item in ranked_public:
                signature = tuple((k, ranked_item["params"][k]) for k in sorted(ranked_item["params"]))
                original = by_signature[signature]
                for score_key in ("comprehensive_score", "tag_match_score", "price_score", "cost_effectiveness_score", "rank", "matched_tags", "missing_tags"):
                    if score_key in ranked_item:
                        original[score_key] = ranked_item[score_key]
                ranked.append(original)
            # Among similarly ranked demand-satisfying schemes, prefer lower contour
            # deviation and lower model anomaly before ordinary recommendation score.
            ranked.sort(key=lambda x: (x["_search_key"][:4], -float(x.get("comprehensive_score", 0) or 0)))
        else:
            ranked = sorted(usable, key=lambda item: item["_search_key"])

        selected = []
        selected_risks = set()
        for item in ranked:
            risk_signature = item.get("_risk_signature")
            if risk_signature and risk_signature in selected_risks:
                rejection["repeated_risk_signature"] += 1
                continue
            if not selected or min(self._normalized_distance(item["params"], existing["params"], definitions) for existing in selected) >= 0.018:
                selected.append(item)
                if risk_signature:
                    selected_risks.add(risk_signature)
            if len(selected) >= count:
                break
        if len(selected) < count:
            for item in ranked:
                if item not in selected:
                    selected.append(item)
                if len(selected) >= count:
                    break
        if not selected and usable:
            selected = usable[:1]

        public_selected = []
        for index, record in enumerate(selected, 1):
            base = record.get("_base") or seeds[0]
            changed = record.get("_changed") or self._changed_parameters(record["params"], base["params"], definitions)
            public_selected.append(self._make_public_item(record, base, changed, index, rng))

        suggestions = []
        if not strict_available:
            suggestions.append("系统已固定可实现的用户技术条件，并返回与剩余需求最接近的探索方案。")
        if any(item.get("extrapolation_warnings") for item in public_selected):
            suggestions.append("部分方案位于历史经验或模型训练范围之外；价格和效能仍已计算，但应按外推结果理解。")
        if rejection["hard_conflict"]:
            suggestions.append("部分用户目标与明确工程硬规则冲突，相关方案只能作为概念探索，不能直接用于工程实施。")

        return {
            "candidates": public_selected,
            "requested_count": count,
            "evaluated_count": evaluations,
            "seed_agreements": [item["agreement_id"] for item in seeds],
            "rejection_statistics": rejection,
            "strict_filter_satisfied": strict_available,
            "strict_candidate_count": len(strict_candidates),
            "best_effort_used": not strict_available,
            "bounds": branch_bounds,
            "relaxation_suggestions": suggestions,
            "generation_method": (
                "deep_extrapolation_multistage_beam_search"
                if deep_search else "batched_directional_beam_search"
            ),
            "search_iterations": iteration,
            "search_mode": "deep_extrapolation" if deep_search else "fast",
        }
