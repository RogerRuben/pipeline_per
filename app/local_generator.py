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

from .anchor_feasibility import assess_explicit_filter_feasibility, validate_anchor_integrity
from .constraint_projection import active_parameter_set, project_constraints
from .coupling_pairs import build_coupling_pairs, exploration_pairs
from .demand_branch import compile_explicit_demand_branches, compile_conflict_core_branches, combine_generation_branches
from .expert_scheme import ExpertSchemeService
from .recommender import rank_agreements
from .requirement_assessment import assess_requirements
from .value_semantics import canonical_filter_value, canonicalize_parameter_value, is_special_value, nice_engineering_step, normal_numeric_values, normalize_boolean, normalize_numeric, special_value_keys, values_equal


def _float(value, default=None):
    result = normalize_numeric(value)
    return result if result is not None else default


def _truth(value):
    return normalize_boolean(value) is True


def _classify_move(search_move):
    text = str(search_move or "")
    if text.startswith("结构调整"):
        return "structural_move"
    if text.startswith("工程联动") or text.startswith("双属性联动"):
        return "coupled_move"
    if text.startswith("单属性"):
        return "single_move"
    if text.startswith("反向轮廓") or text.startswith("兜底"):
        return "output_target_move"
    if text.startswith("相关联合"):
        return "coupled_move"
    if text.startswith("深度精英交叉"):
        return "elite_crossover"
    return "single_move"


def _move_reason_type(move_type):
    return {
        "structural_move": "structural_change",
        "coupled_move": "engineering_coupling",
        "single_move": "local_tuning",
        "output_target_move": "output_target",
        "elite_crossover": "elite_recombination",
    }.get(move_type, "local_tuning")


def _move_reason_text(search_move):
    move_type = _classify_move(search_move)
    return {
        "structural_move": "改变产品结构条件（控制属性联动）",
        "coupled_move": "按工程耦合关系联动调整",
        "single_move": "局部参数调整",
        "output_target_move": "逼近输出目标（价格/效能）",
        "elite_crossover": "重组精英方案的有效改动",
    }.get(move_type, "参数搜索动作")


def _change_source(key, projection_repairs, locked):
    if any((rep or {}).get("parameter") == key for rep in (projection_repairs or [])):
        return "constraint_projection"
    if key in set(locked or []):
        return "user_frozen"
    return "search"


def _change_reason_type(key, projection_repairs, locked):
    if any((rep or {}).get("parameter") == key for rep in (projection_repairs or [])):
        for rep in (projection_repairs or []):
            if (rep or {}).get("parameter") == key:
                return rep.get("type") or "conditional_projection"
    if key in set(locked or []):
        return "user_locked"
    return "search_move"


def _clamp(value, lo, hi):
    return max(float(lo), min(float(hi), float(value)))


def build_step_schedule(mode, max_rounds):
    """Return a step-scale schedule for a user-configurable round count.

    Fast mode decays from broad to fine; deep mode widens on round two then
    decays.  The endpoints match the historical fixed schedules.
    """
    rounds = max(1, int(max_rounds))
    if mode == "deep":
        if rounds <= 2:
            return [0.52, 0.90][:rounds]
        tail_count = rounds - 2
        start, end = 0.68, 0.13
        tail = [start * (end / start) ** (i / max(tail_count - 1, 1)) for i in range(tail_count)]
        return [0.52, 0.90] + tail
    if rounds <= 1:
        return [0.52]
    start, end = 0.52, 0.09
    return [start * (end / start) ** (i / (rounds - 1)) for i in range(rounds)]


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


def _nearest_numeric_business_value(candidates, current):
    """Compare through floats while returning the original business value."""
    current_num = _float(current)
    if current_num is None:
        return None
    projected = [(candidate, _float(candidate)) for candidate in candidates]
    projected = [item for item in projected if item[1] is not None]
    return min(projected, key=lambda item: abs(item[1] - current_num))[0] if projected else None


def merge_bounds(base, extra):
    """Merge tag suggestions with authoritative direct-user anchors.

    ``base`` contains tag-derived suggestions and ``extra`` contains explicit
    user filters. A conflict never invents a midpoint: the direct constraint
    survives unchanged and the tag conflict is retained as diagnostics.
    """
    result = dict((key, dict(value)) for key, value in (base or {}).items())
    for key, direct in (extra or {}).items():
        direct = dict(direct)
        lower = direct.get("min", -1e99)
        upper = direct.get("max", 1e99)
        if lower > upper or ("allowed" in direct and not direct["allowed"]):
            direct.update({"conflict": True, "conflict_reason": "explicit_filters_mutually_inconsistent",
                           "requested_min": lower, "requested_max": upper})
            result[key] = direct
            continue
        tag = result.get(key)
        if not tag:
            result[key] = direct
            continue
        merged = dict(tag)
        if direct.get("min") is not None:
            merged["min"] = max(float(direct["min"]), float(merged.get("min", direct["min"])))
        if direct.get("max") is not None:
            merged["max"] = min(float(direct["max"]), float(merged.get("max", direct["max"])))
        if direct.get("allowed") is not None:
            values = list(direct["allowed"])
            merged["allowed"] = values if "allowed" not in merged else [x for x in merged["allowed"] if x in values]
        merged_conflict = merged.get("min", -1e99) > merged.get("max", 1e99) or ("allowed" in merged and not merged["allowed"])
        if merged_conflict:
            direct.update({"conflict": True, "conflict_reason": "tag_direct_conflict",
                           "tag_suggestion": dict(tag), "direct_user_wins": True})
            result[key] = direct
        else:
            result[key] = merged
    return result


def filters_to_anchors(filters, definitions=None, mode="all"):
    """Compile explicit user filters into generator demand anchors.

    This is the generation-side counterpart of ``RequirementAssessment``:
    numeric rules become min/max boxes, while enums remain canonical business
    values.  Business-to-model encoding happens only in Store.runtime_parameters
    immediately before a model call.
    """
    # A multi-rule OR is compiled into independent branches before this function
    # is called.  A one-rule OR is equivalent to its single explicit anchor.
    if mode != "all" and len(filters or []) > 1:
        raise ValueError("多个 OR 条件必须先编译为独立 Demand Branch")
    definitions = definitions or {}
    result = {}
    for rule in filters or []:
        key, op = rule.get("parameter_id"), rule.get("operator")
        if not key:
            continue
        definition = definitions.get(key) or {}
        item = result.setdefault(key, {})
        v1, v2 = _float(rule.get("value1")), _float(rule.get("value2"))
        if op == "special_is" and is_special_value(definition, rule.get("value1")):
            item["allowed"] = [rule.get("value1")]
        elif op == "gte" and v1 is not None:
            item["min"] = max(v1, float(item.get("min", v1)))
        elif op == "gt" and v1 is not None:
            item["min"] = max(v1 + 1e-7, float(item.get("min", v1 + 1e-7)))
        elif op == "lte" and v1 is not None:
            item["max"] = min(v1, float(item.get("max", v1)))
        elif op == "lt" and v1 is not None:
            item["max"] = min(v1 - 1e-7, float(item.get("max", v1 - 1e-7)))
        elif op == "eq":
            business = rule.get("value1")
            numeric = _float(business)
            if numeric is not None:
                item["min"] = item["max"] = numeric
            else:
                item["allowed"] = [business]
        elif op == "boolean_is":
            canonical = canonical_filter_value(rule.get("value1"), definition)
            if isinstance(canonical, bool):
                item["min"] = item["max"] = 1.0 if canonical else 0.0
            else:
                item["allowed"] = [rule.get("value1")]
        elif op == "text_equals":
            item["allowed"] = [rule.get("value1")]
        elif op == "range_inside" and v1 is not None and v2 is not None:
            item["min"], item["max"] = min(v1, v2), max(v1, v2)
        elif not item:
            result.pop(key, None)
    return result


def filters_to_bounds(filters, mode="all"):
    """Backward-compatible wrapper (definitions are optional in older callers)."""
    return filters_to_anchors(filters, None, mode)


class HistorySeededGenerator(object):
    def __init__(self, store, runtime, evaluate_callback, evaluate_batch_callback=None):
        self.store = store
        self.runtime = runtime
        self.evaluate_callback = evaluate_callback
        self.evaluate_batch_callback = evaluate_batch_callback

    def _parameter_definitions(self):
        return self.store.parameter_map()

    def _request_distance(self, item, request, definitions, tag_weights):
        # One requirement semantics everywhere: the seed's distance from the user's
        # conditions is the shared RequirementAssessment penalty, so OR-groups and
        # unknown model outputs are handled identically to recommendation ranking.
        assessment = assess_requirements(item, request, definitions, self.store.tag_map(),
                                         getattr(self.store, "constraint_rows", lambda: [])())
        return assessment["demand_penalty"]

    def select_seeds(self, request, limit=12, historical=None):
        historical = list(historical if historical is not None else self.store.historical_agreements())
        definitions = self._parameter_definitions()
        tag_weights = dict((key, value.get("weight", 1.0)) for key, value in self.store.tag_map().items())
        policy = request.get("_scenario_policy") or {}
        weights = policy.get("ranking_weights") or {}
        if weights:
            def outcome(item, key, fallback=None):
                value = item.get(key)
                if value in (None, ""):
                    value = (item.get("evaluation") or {}).get(key)
                if value in (None, "") and fallback:
                    value = item.get(fallback)
                return _float(value)
            prices = [outcome(item, "predicted_price_wan", "historical_price_wan") for item in historical]
            capabilities = [outcome(item, "capability_score") for item in historical]
            valid_prices = [value for value in prices if value is not None]
            valid_capabilities = [value for value in capabilities if value is not None]
            pmin, pmax = (min(valid_prices), max(valid_prices)) if valid_prices else (0.0, 0.0)
            cmin, cmax = (min(valid_capabilities), max(valid_capabilities)) if valid_capabilities else (0.0, 0.0)
            scored = []
            for index, item in enumerate(historical):
                assessment = assess_requirements(
                    item, request, definitions, self.store.tag_map(),
                    getattr(self.store, "constraint_rows", lambda: [])(),
                )
                technical_conditions = [
                    condition for condition in assessment.get("conditions") or []
                    if condition.get("kind") in ("parameter", "tag")
                ]
                technical_distance = (
                    sum(float(condition.get("gap") if condition.get("gap") is not None else (0 if condition.get("matched") else 1)) for condition in technical_conditions)
                    / max(len(technical_conditions), 1)
                )
                price = prices[index]
                price_distance = 0.5 if price is None else (0.0 if pmax == pmin else (price - pmin) / (pmax - pmin))
                capability = capabilities[index]
                capability_distance = 0.5 if capability is None else (0.0 if cmax == cmin else (cmax - capability) / (cmax - cmin))
                preference_distance = (
                    float(weights.get("technical", 0)) * technical_distance +
                    float(weights.get("price", 0)) * price_distance +
                    float(weights.get("capability", 0)) * capability_distance
                )
                scored.append((float(assessment.get("demand_penalty") or 0) + preference_distance, item))
        else:
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
            if is_special_value(definition, a[key]) or is_special_value(definition, b[key]):
                values.append(0.0 if self._value_equal(a[key], b[key], definition) else 1.0)
                continue
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
            vals = [float(item["params"][key]) for item in seeds if item.get("params", {}).get(key) not in (None, "") and not is_special_value(definitions[key], item["params"][key])]
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
                    and not is_special_value(definitions[key_i], x["params"][key_i])
                    and not is_special_value(definitions[key_j], x["params"][key_j])
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
        if value_type in ("enum", "text"):
            return "unordered_enum"
        # Numeric allowed values are observations/advice unless DataMaster
        # explicitly declares ordered_discrete. They must never snap an explicit
        # user anchor back into an old historical range.
        if value_type in ("integer", "ip_grade"):
            return "integer"
        if value_type in ("number", "float", "continuous"):
            return "continuous"
        return "continuous"

    def _normalized_allowed_values(self, definition):
        values = self._allowed_values(definition)
        kind = self._search_type(definition)
        if kind in ("continuous", "integer", "ordered_discrete"):
            values = normal_numeric_values(definition, values)
        if kind == "ordered_discrete":
            projected = [(value, _float(value)) for value in values]
            if values and all(number is not None for _value, number in projected):
                result = []
                seen = set()
                for value, number in sorted(projected, key=lambda item: item[1]):
                    if number not in seen:
                        result.append(value)
                        seen.add(number)
                return result
        return list(values)

    def _value_equal(self, left, right, definition):
        return values_equal(left, right, definition)

    def _attribute_move_distance(self, current, candidate, definition):
        """Normalized one-attribute move size used to prefer minimal changes.

        When several neighbors already satisfy an output target, the generator
        should keep the closest legal value rather than jumping straight to an
        extreme bound. This makes paths such as IP65 -> IP64 discoverable and
        explainable.
        """
        kind = self._search_type(definition)
        if is_special_value(definition, current) or is_special_value(definition, candidate):
            return 0.0 if self._value_equal(current, candidate, definition) else 1.0
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
        declared_specials = special_value_keys(definition)
        if is_special_value(definition, current):
            starters = normal_numeric_values(definition, self._allowed_values(definition))
            default = definition.get("default_value", definition.get("business_default"))
            if default not in (None, "") and not is_special_value(definition, default):
                starters.insert(0, default)
            return list(dict.fromkeys(starters))
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
            raw_step = max(float(std_value or 0.0) * max(step_scale, 0.20), span * 0.025, 10 ** (-max(1, int(definition.get("decimal_places", 3)))))
            step = nice_engineering_step(raw_step, int(definition.get("decimal_places", 3) or 3)) or raw_step
            raw = [current_num - step, current_num + step, current_num - 2 * step, current_num + 2 * step,
                   current_num - 3 * step, current_num + 3 * step]
            if include_bounds:
                raw.extend([lower, upper, lower + span * 0.25, lower + span * 0.50, lower + span * 0.75])
            values = [_clamp(value, lower, upper) for value in raw]
        unique = []
        for value in values:
            if not self._value_equal(value, current, definition) and not any(self._value_equal(value, existing, definition) for existing in unique):
                unique.append(value)
        for special in declared_specials:
            if not any(self._value_equal(special, existing, definition) for existing in unique):
                unique.append(canonicalize_parameter_value(definition, special))
        return unique

    def _anchor_demands(self, params, bounds, definitions):
        """Project user requirements first and return immutable anchor values.

        DataMaster min/max are only a default search envelope.  Explicit user
        requirements remain authoritative and are never clamped to that
        potentially stale reference metadata.
        """
        locked = {}
        conflicts = []
        for key, rule in bounds.items():
            if key not in definitions:
                continue
            definition = definitions[key]
            if not definition.get("auto_adjustable", 1):
                conflicts.append({"parameter_id": key, "reason": "该属性被设置为不允许自动调整"})
                continue
            allowed = rule.get("allowed")
            if allowed:
                explicit_special = next((candidate for candidate in allowed if is_special_value(definition, candidate)), None)
                if explicit_special is not None:
                    value = canonicalize_parameter_value(definition, explicit_special)
                    locked[key] = value
                    params[key] = value
                    continue
                numeric_allowed = [candidate for candidate in allowed if _float(candidate) is not None]
                string_allowed = [candidate for candidate in allowed if _float(candidate) is None]
                current = params.get(key)
                current_num = _float(current)
                if numeric_allowed and current_num is not None:
                    value = _nearest_numeric_business_value(numeric_allowed, current)
                elif any(self._value_equal(current, candidate, definition) for candidate in allowed):
                    value = current
                elif string_allowed:
                    value = string_allowed[0]
                elif numeric_allowed:
                    value = numeric_allowed[0]
                else:
                    value = current if current is not None else allowed[0]
            else:
                requested_lo = float(rule.get("min", -1e99))
                requested_hi = float(rule.get("max", 1e99))
                lower = requested_lo
                upper = requested_hi
                current = params.get(key)
                current_num = _float(current)
                has_min = "min" in rule
                has_max = "max" in rule
                if current_num is None:
                    # The user explicitly requested this field; create it from the
                    # demand rather than leaving the seed missing and failing at
                    # model-input time.
                    if lower <= upper:
                        if has_min and has_max:
                            value = (float(rule["min"]) + float(rule["max"])) / 2.0
                        elif has_min:
                            value = float(rule["min"])
                        elif has_max:
                            value = float(rule["max"])
                        else:
                            value = 0.0
                    else:
                        value = requested_lo
                        conflicts.append({
                            "parameter_id": key,
                            "reason": "用户显式筛选条件自身互相矛盾",
                            "requested_min": requested_lo,
                            "requested_max": requested_hi,
                            "resolution": "explicit_filter_conflict",
                        })
                elif lower <= upper:
                    # Minimal-change projection: retain a satisfying seed value;
                    # otherwise move exactly to the nearest requested boundary.
                    if current_num < lower:
                        value = lower
                    elif current_num > upper:
                        value = upper
                    else:
                        value = current_num
                else:
                    value = requested_lo
                    conflicts.append({
                        "parameter_id": key,
                        "reason": "用户显式筛选条件自身互相矛盾",
                        "requested_min": requested_lo,
                        "requested_max": requested_hi,
                        "resolution": "explicit_filter_conflict",
                    })
            kind = self._search_type(definition)
            if is_special_value(definition, value):
                locked[key] = canonicalize_parameter_value(definition, value)
                params[key] = locked[key]
                continue
            if kind == "boolean":
                if allowed is not None and any(_float(x) not in (0.0, 1.0) for x in allowed):
                    if _float(value) is not None:
                        value = float(value)
                else:
                    value = 1 if float(value) >= 0.5 else 0
            elif kind == "integer":
                value = int(round(float(value)))
            elif kind == "ordered_discrete":
                allowed_values = self._normalized_allowed_values(definition)
                if allowed_values:
                    value = _nearest_numeric_business_value(allowed_values, value)
            locked[key] = value
            params[key] = value
        return locked, conflicts

    @staticmethod
    def _restore_locked(params, locked):
        for key, value in (locked or {}).items():
            params[key] = value
        return params

    @staticmethod
    def _apply_frozen(params, locked, frozen_parameters):
        """Lock user-frozen parameters to each seed's own historical value.

        Returns a ``{key: source}`` map so the trace can distinguish user-anchored
        values from user-frozen values, while every search action treats both as
        immutable via the shared ``locked`` dict.
        """
        locked_sources = dict((key, "user_anchor") for key in (locked or {}))
        for key in (frozen_parameters or []):
            if key in params and key not in locked:
                locked[key] = params[key]
                locked_sources[key] = "user_frozen"
        return locked_sources

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

    def _repair_relations(self, params, reference, definitions, locked):
        """Repair explicit relations while preserving every user-anchored value.

        ``reference`` is the search centre the current move was generated from
        (the parent candidate in iterative rounds, or the historical seed in the
        first round).  Local compensation therefore measures the current move
        relative to that parent, so a previous round's valid change is never
        silently pulled back toward the historical seed.
        """
        repairs = []
        for item in self.store.coupling_rows():
            a, b = item.get("parameter_a"), item.get("parameter_b")
            if a not in params or b not in params or a not in reference or b not in reference or a not in definitions or b not in definitions:
                continue
            coupling_type = item.get("coupling_type")
            da, db = float(params[a]) - float(reference[a]), float(params[b]) - float(reference[b])
            if coupling_type in ("positive", "negative"):
                expected_sign = 1.0 if coupling_type == "positive" else -1.0
                a_moved = abs(da) > 1e-9
                b_moved = abs(db) > 1e-9
                if not a_moved and not b_moved:
                    continue
                if a_moved and not b_moved:
                    violated = True
                elif b_moved and not a_moved:
                    violated = True
                else:
                    violated = da * db * expected_sign < 0
                if not violated:
                    continue
                span_a = max(float(definitions[a].get("max_value") or 1) - float(definitions[a].get("min_value") or 0), 1e-9)
                span_b = max(float(definitions[b].get("max_value") or 1) - float(definitions[b].get("min_value") or 0), 1e-9)
                strength = max(float(item.get("strength") if item.get("strength") is not None else 0.35), 1e-6)
                a_locked = a in locked or not definitions.get(a, {}).get("auto_adjustable", 1)
                b_locked = b in locked or not definitions.get(b, {}).get("auto_adjustable", 1)
                # Symmetric local compensation: whichever side the current move
                # touched, the untouched side follows it (relative to the parent
                # centre), unless that side is locked.
                if a_moved and not b_moved:
                    if not b_locked:
                        params[b] = float(reference[b]) + expected_sign * strength * (da / span_a) * span_b
                        repairs.append(item.get("coupling_id"))
                elif b_moved and not a_moved:
                    if not a_locked:
                        params[a] = float(reference[a]) + expected_sign * (db / span_b) * span_a / strength
                        repairs.append(item.get("coupling_id"))
                else:
                    # Both sides moved but in the wrong relative direction; fix
                    # the unlocked side, preferring B (the historical follower).
                    if not b_locked:
                        params[b] = float(reference[b]) + expected_sign * strength * (da / span_a) * span_b
                        repairs.append(item.get("coupling_id"))
                    elif not a_locked:
                        params[a] = float(reference[a]) + expected_sign * (db / span_b) * span_a / strength
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
        from .conditional_constraint import TEMPLATE_KIND_V2, parse_template_metadata
        for rule in self.store.constraint_rows():
            # V2 placeholder rows are storage/group identity only; the real
            # relationship is executed exclusively by project_constraints().
            if parse_template_metadata(rule).get("template") == TEMPLATE_KIND_V2:
                continue
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
            if source not in reference or target not in reference or source not in definitions or target not in definitions:
                continue
            source_delta = float(params[source]) - float(reference[source])
            target_delta = float(params[target]) - float(reference[target])
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
            params[target] = float(reference[target]) + adjustment
            repairs.append("direction_prior:%s->%s" % (source, target))
        self._restore_locked(params, locked)
        return repairs

    def _round_values(self, params, definitions, locked=None):
        for key, definition in definitions.items():
            if key not in params:
                continue
            kind = self._search_type(definition)
            if is_special_value(definition, params[key]):
                continue
            allowed = self._normalized_allowed_values(definition)
            if kind in ("ordered_discrete", "unordered_enum") and allowed:
                if kind == "ordered_discrete":
                    current = _float(params[key])
                    if current is not None:
                        params[key] = _nearest_numeric_business_value(allowed, current)
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
            "boolean_is": "为", "special_is": "状态为", "text_equals": "等于", "text_contains": "包含",
            "range_inside": "位于区间", "range_contains": "覆盖区间", "range_overlap": "与区间相交",
        }.get(operator, operator)

    def _generation_branches(self, request, definitions=None):
        definitions = definitions or self._parameter_definitions()
        filters = list(request.get("indicator_filters") or [])
        explicit = compile_explicit_demand_branches(
            filters, request.get("indicator_filter_mode", "all"), definitions
        )
        # When a true AND box is internally contradictory, preserve the original
        # AND assessment but explore each explicit direction as Best Effort.
        if str(request.get("indicator_filter_mode") or "all") == "all" and len(filters) > 1:
            joint = filters_to_anchors(filters, definitions, "all")
            explicit_conflicts = self._detect_explicit_cross_conflicts(filters, definitions, joint)
            conflict_key_groups = [
                {key} for key, value in joint.items() if value.get("conflict")
            ] + [set(item.get("parameter_ids") or []) for item in explicit_conflicts]
            conflict_key_groups = [group for group in conflict_key_groups if group]
            if conflict_key_groups:
                conflict_branches = compile_conflict_core_branches(
                    filters, conflict_key_groups, definitions, max_branches=24
                )
                for branch in conflict_branches:
                    branch["explicit_conflicts"] = explicit_conflicts
                explicit = conflict_branches
        tags = self.store.tag_rule_branches(request.get("selected_tags"), max_branches=24)
        return combine_generation_branches(explicit, tags, max_branches=24)

    @staticmethod
    def _numeric_bounds_interval(bounds, key):
        rule_bounds = (bounds or {}).get(key) or {}
        allowed = [_float(value) for value in rule_bounds.get("allowed") or []]
        allowed = [value for value in allowed if value is not None]
        if allowed:
            return min(allowed), max(allowed)
        return (
            _float(rule_bounds.get("min"), float("-inf")),
            _float(rule_bounds.get("max"), float("inf")),
        )

    @classmethod
    def _affine_relation_impossible(cls, bounds, left, right, multiplier, offset, operator):
        left_lo, left_hi = cls._numeric_bounds_interval(bounds, left)
        right_lo, right_hi = cls._numeric_bounds_interval(bounds, right) if right else (0.0, 0.0)
        multiplier, offset = float(multiplier), float(offset)
        if multiplier >= 0:
            diff_lo, diff_hi = left_lo - multiplier * right_hi - offset, left_hi - multiplier * right_lo - offset
        else:
            diff_lo, diff_hi = left_lo - multiplier * right_lo - offset, left_hi - multiplier * right_hi - offset
        return {
            "gte": diff_hi < 0, "gt": diff_hi <= 0, "lte": diff_lo > 0,
            "lt": diff_lo >= 0, "eq": diff_lo > 0 or diff_hi < 0,
        }.get(operator, False)

    def _detect_explicit_cross_conflicts(self, filters, definitions, joint_bounds=None):
        """Detect hard cross-parameter conflicts in an explicit AND request.

        Single-field interval contradictions remain the responsibility of
        ``filters_to_anchors``.  This pass anchors a representative point while
        locking every explicitly requested field, then asks the configured
        conditional and hard affine relations whether that point can be kept.
        It deliberately ignores advisory rules.
        """
        filters = list(filters or [])
        explicit_keys = set(rule.get("parameter_id") for rule in filters if rule.get("parameter_id"))
        if len(explicit_keys) < 2:
            return []
        bounds = joint_bounds if joint_bounds is not None else filters_to_anchors(filters, definitions, "all")
        rules = getattr(self.store, "constraint_rows", lambda: [])()
        conflicts = []

        def requested_accepts(key, candidates=None, interval=None):
            requested = bounds.get(key) or {}
            definition = definitions.get(key) or {}
            allowed = requested.get("allowed") or []
            if candidates is not None:
                requested_lo, requested_hi = self._numeric_bounds_interval(bounds, key)
                for candidate in candidates:
                    if allowed and not any(self._value_equal(candidate, value, definition) for value in allowed):
                        continue
                    candidate_num = _float(candidate)
                    if candidate_num is not None and not (requested_lo <= candidate_num <= requested_hi):
                        continue
                    return True
                return False
            requested_lo, requested_hi = self._numeric_bounds_interval(bounds, key)
            return interval is not None and max(requested_lo, interval[0]) <= min(requested_hi, interval[1])

        # Conditional metadata is the source of truth.  Test domain intersection,
        # not one representative point, so a broad request such as target<=5 is
        # not falsely declared incompatible with an inactive value of -1.
        from .conditional_constraint import TEMPLATE_KIND, TEMPLATE_KIND_V2, parse_template_metadata
        seen_groups = set()
        for rule in rules:
            group = rule.get("constraint_group")
            if not group or group in seen_groups:
                continue
            meta = parse_template_metadata(rule)
            if meta.get("template") not in (TEMPLATE_KIND, TEMPLATE_KIND_V2):
                continue
            seen_groups.add(group)
            controller, target = meta.get("controller"), meta.get("target")
            if controller not in explicit_keys or target not in explicit_keys:
                continue
            controller_bounds = bounds.get(controller) or {}
            controller_allowed = list(controller_bounds.get("allowed") or [])
            when_value = (meta.get("when") or {}).get("model_value") if meta.get("template") == TEMPLATE_KIND_V2 else meta.get("active_value", 1)
            if controller_allowed:
                when_possible = any(self._value_equal(value, when_value, definitions.get(controller) or {}) for value in controller_allowed)
                otherwise_possible = any(not self._value_equal(value, when_value, definitions.get(controller) or {}) for value in controller_allowed)
            else:
                when_num = _float(when_value)
                lo, hi = self._numeric_bounds_interval(bounds, controller)
                when_possible = when_num is not None and lo <= when_num <= hi
                otherwise_possible = not (when_num is not None and lo == hi == when_num)

            def branch_compatible(branch):
                mode = branch.get("mode") or "not_applicable"
                if mode in ("not_applicable", "fixed"):
                    return requested_accepts(target, candidates=[branch.get("model_value")])
                if mode == "enum":
                    return requested_accepts(target, candidates=branch.get("allowed") or [])
                if mode == "range":
                    lo, hi = float(branch.get("min", 0)), float(branch.get("max", 1))
                    return requested_accepts(target, interval=(min(lo, hi), max(lo, hi)))
                return True

            if meta.get("template") == TEMPLATE_KIND_V2:
                when_ok = branch_compatible(meta.get("then") or {})
                otherwise_ok = branch_compatible(meta.get("otherwise") or {})
            else:
                inactive = float(meta.get("inactive_value", -1))
                when_ok = requested_accepts(target, interval=(float(meta["active_min"]), float(meta["active_max"])))
                otherwise_ok = requested_accepts(target, candidates=[inactive])
            if not ((when_possible and when_ok) or (otherwise_possible and otherwise_ok)):
                conflicts.append({
                    "type": "explicit_conditional_conflict", "source": "conditional_constraint",
                    "constraint_group": group, "controller": controller, "parameter": target,
                    "parameter_ids": [controller, target],
                    "reason": "显式技术条件与条件属性关系冲突",
                })

        for rule in rules:
            if str(rule.get("severity") or "warning") != "error":
                continue
            if rule.get("rule_kind") in ("conditional_lower", "conditional_upper"):
                continue
            left, right = rule.get("left_parameter"), rule.get("right_parameter")
            if left not in explicit_keys or (right and right not in explicit_keys):
                continue
            multiplier = float(rule.get("multiplier") or 1)
            offset = float(rule.get("offset") or 0)
            operator = rule.get("operator")
            if self._affine_relation_impossible(bounds, left, right, multiplier, offset, operator):
                conflicts.append({
                    "type": "explicit_hard_relation_conflict", "source": "constraint_rule",
                    "rule_id": rule.get("rule_id"), "left_parameter": left,
                    "right_parameter": right, "parameter_ids": [left] + ([right] if right else []),
                    "reason": rule.get("message") or "显式技术条件与工程硬规则冲突",
                })

        for relation in getattr(self.store, "coupling_rows", lambda: [])():
            if relation.get("coupling_type") != "feasible_domain":
                continue
            if str(relation.get("severity") or "warning") != "error":
                continue
            parameter_a, parameter_b = relation.get("parameter_a"), relation.get("parameter_b")
            if parameter_a not in explicit_keys or parameter_b not in explicit_keys:
                continue
            if self._affine_relation_impossible(
                bounds, parameter_b, parameter_a,
                float(relation.get("multiplier") or 1), float(relation.get("offset") or 0),
                relation.get("domain_operator") or "gte",
            ):
                conflicts.append({
                    "type": "explicit_hard_coupling_conflict", "source": "feasible_domain_coupling",
                    "coupling_id": relation.get("coupling_id"), "parameter_a": parameter_a,
                    "parameter_b": parameter_b, "parameter_ids": [parameter_a, parameter_b],
                    "reason": relation.get("message") or "显式技术条件与工程可行域冲突",
                })
        return conflicts

    def _compile_generation_branch(self, request, branch, definitions=None):
        branch = branch or {"tag_rules": [], "explicit_filters": list(request.get("indicator_filters") or []),
                            "explicit_filter_mode": request.get("indicator_filter_mode", "all"),
                            "branch_id": "BRANCH-ALL", "demand_branch_id": "BRANCH-ALL",
                            "title": "按当前综合需求探索", "summary": "按当前综合需求探索。"}
        technical = []
        branch_request = dict(request)
        branch_request["indicator_filters"] = list(branch.get("assessment_filters") or branch.get("explicit_filters") or [])
        branch_request["indicator_filter_mode"] = "all"
        for rule in branch.get("tag_rules") or []:
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
        definitions = definitions or self._parameter_definitions()
        bounds = merge_bounds(
            filters_to_anchors(technical, definitions, "all"),
            filters_to_anchors(branch.get("explicit_filters") or [], definitions, "all"),
        )
        return bounds, branch_request, {
            "branch_id": branch.get("branch_id") or "BRANCH-ALL",
            "demand_branch_id": branch.get("demand_branch_id") or "BRANCH-ALL",
            "title": branch.get("title") or "按当前综合需求探索",
            "summary": branch.get("summary") or "按当前综合需求探索。",
            "anchor_filters": list(branch.get("explicit_filters") or []),
            "tag_rule_groups": dict(branch.get("tag_groups") or {}),
            "unresolved_tags": list(branch.get("unresolved_tags") or []),
            "compiled_rule_count": len(branch.get("tag_rules") or []),
            "explicit_conflicts": list(branch.get("explicit_conflicts") or []),
        }

    def _tag_branch_for_seed(self, request, seed_index):
        """Backward-compatible entry point backed by the unified branch compiler."""
        definitions = self._parameter_definitions()
        branches = self._generation_branches(request, definitions)
        branch = branches[seed_index % max(len(branches), 1)] if branches else None
        return self._compile_generation_branch(request, branch, definitions)

    @staticmethod
    def _output_target_penalty(evaluation, request):
        pieces = []
        checks = [
            ("max_price", evaluation.get("predicted_price_wan"), "max", 4.0),
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
        """Assess user requirements via the shared RequirementAssessment module.

        Returns ``(unmet_texts, penalty, assessment)`` so the caller can attach the
        structured ``requirement_assessment`` for the UI while keeping the existing
        human-readable ``unmet_conditions`` contract.
        """
        assessment = assess_requirements(item, request, definitions, tag_map,
                                          getattr(self.store, "constraint_rows", lambda: [])())
        unmet = []
        failed_rule_labels = []
        indicator_logic = assessment.get("indicator_logic")
        for condition in assessment["conditions"]:
            kind = condition.get("kind")
            if condition.get("status") != "unmatched":
                continue
            if kind == "parameter":
                # Individual rule detail; reported together with the group verdict.
                failed_rule_labels.append(condition.get("label", ""))
                continue
            if kind == "tag":
                unmet.append("缺少标签“%s”" % condition.get("label", ""))
                continue
            label = condition.get("label", "")
            actual = condition.get("actual")
            unmet.append("%s（当前 %s）" % (label, actual) if actual is not None else label)
        if indicator_logic and not indicator_logic.get("satisfied") and failed_rule_labels:
            mode = indicator_logic.get("mode") or "all"
            unmet.append("技术指标条件（%s）%s" % ("任一满足即可" if mode == "any" else "需全部满足", "；".join(failed_rule_labels) or "存在未满足项"))
        return unmet, assessment["demand_penalty"], assessment

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
                "reject_hard_violation": "模型服务返回明确硬约束违反",
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
        capability = float(evaluation.get("capability_score") or 0) / 100.0
        feasibility = float(evaluation.get("feasibility_probability") or 0)
        ce = float(evaluation.get("cost_effectiveness") or 0)
        from .recommender import effective_price
        price = effective_price(evaluation)
        price_utility = 1.0 / (1.0 + max(price, 0.0) / 20.0)
        ce_utility = ce / (1.0 + abs(ce))
        gate_penalty = 1.5 if (evaluation.get("physical_gate") or {}).get("passed") is False else 0.0
        return 0.38 * capability + 0.27 * feasibility + 0.18 * ce_utility + 0.17 * price_utility - gate_penalty

    def _record_from_params(self, params, base, request, definitions, tag_map, locked, anchor_conflicts,
                            repairs, soft_moves, iteration, attempt, search_move, evaluation=None, parent_record=None,
                            constraint_conflicts=None, inactive_parameters=None, projection_repairs=None):
        # Params arrive already canonicalized by _finalize_params; here we only
        # recompute the active set for bookkeeping, never mutate the model input.
        constraint_rules = getattr(self.store, "constraint_rows", lambda: [])()
        active_set = active_parameter_set(params, definitions, constraint_rules, locked=locked)
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
        demand_unmet, demand_penalty, requirement_assessment = self._demand_assessment(item, request, definitions, tag_map)
        hard_conflicts, hard_penalty = self._engineering_conflicts(evaluation)
        extrapolation, contour_penalty, anomaly_penalty = self._extrapolation_assessment(evaluation, contour_details)
        seed_distance = self._normalized_distance(params, base["params"], definitions)
        objective = self._objective_value(evaluation)
        move_type = _classify_move(search_move)
        node_id = "N%03d-%03d" % (int(iteration), int(attempt))
        parent_node_id = (parent_record or {}).get("generation_trace", {}).get("node_id")
        move_changes = self._changed_parameters(params, (parent_record or {}).get("params") or base["params"], definitions) if parent_record else self._changed_parameters(params, base["params"], definitions)
        prior_params = (parent_record or {}).get("params") or base["params"]
        special_transition = any(
            key in params and key in prior_params
            and is_special_value(definition, params[key]) != is_special_value(definition, prior_params[key])
            for key, definition in definitions.items()
        )
        item.update({
            "unmet_conditions": demand_unmet + hard_conflicts,
            "demand_unmet_conditions": demand_unmet,
            "requirement_assessment": requirement_assessment,
            "engineering_conflicts": hard_conflicts,
            "constraint_conflicts": constraint_conflicts or [],
            "inactive_parameters": inactive_parameters if inactive_parameters is not None else active_set["inactive_parameters"],
            "active_parameters": active_set["active_parameters"],
            "projection_repairs": projection_repairs or [],
            "structural_move": bool(
                (isinstance(search_move, str) and search_move.startswith("结构调整"))
                or special_transition
                or any(rep.get("type") in ("conditional_activation", "conditional_deactivation") for rep in (projection_repairs or []))
            ),
            "extrapolation_warnings": extrapolation,
            "contour_extrapolation": any(x.get("state") != "inside" for x in contour_details),
            "fit_penalty": round(demand_penalty + 2.5 * hard_penalty, 6),
            "strict_filter_satisfied": requirement_assessment["strict_satisfied"] and not hard_conflicts,
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
                "seed_source": base.get("agreement_source") or base.get("seed_source") or "historical",
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
                "node_id": node_id,
                "parent_node_id": parent_node_id,
                "move_type": move_type,
                "move": {
                    "type": move_type,
                    "reason_type": _move_reason_type(move_type),
                    "reason_text": _move_reason_text(search_move),
                    "changes": [
                        {
                            "parameter_id": key,
                            "before": (parent_record or {}).get("params", {}).get(key),
                            "after": params.get(key),
                            "source": _change_source(key, projection_repairs, locked),
                            "reason_type": _change_reason_type(key, projection_repairs, locked),
                        }
                        for key in move_changes
                    ],
                },
                "origin_seed_id": base["agreement_id"],
                "parent_iteration": (parent_record or {}).get("generation_trace", {}).get("iteration"),
                "parent_attempt": (parent_record or {}).get("generation_trace", {}).get("attempt"),
                "parameters_before_move": dict((parent_record or {}).get("params") or {}),
                "changed_from_parent": self._changed_parameters(params, (parent_record or {}).get("params") or {}, definitions) if parent_record else [],
                "changed_from_origin": self._changed_parameters(params, base["params"], definitions),
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
        # Lexicographic comparison follows the agreed priority order.  A
        # structural move (changing a conditional controller) carries a small
        # complexity penalty so the search prefers parameter tuning over removing
        # a functional module when both are close to the target.
        structural_penalty = 0.35 if item.get("structural_move") else 0.0
        item["_search_key"] = (
            round(hard_penalty, 8),
            round(demand_penalty, 8),
            round(structural_penalty, 8),
            round(contour_penalty, 8),
            round(anomaly_penalty, 8),
            round(-objective, 8),
            round(seed_distance, 8),
        )
        item["generation_level"] = "strict" if item["strict_filter_satisfied"] else "best_effort"
        return item

    def _exploratory_record(self, params, base, request, definitions, locked, iteration, attempt, search_move, error):
        """Keep a parameter proposal when every model service rejects evaluation."""
        constraint_rules = getattr(self.store, "constraint_rows", lambda: [])()
        active_set = active_parameter_set(params, definitions, constraint_rules, locked=locked)
        item = {
            "agreement_id": "", "product_code": self.runtime.schema.get("product_code"),
            "agreement_name": "", "positioning": self.store._positioning(base.get("tags") or []),
            "agreement_source": "live_generated", "source_year": time.localtime().tm_year,
            "supplier_type": "智能探索参数方案", "historical_price_wan": None,
            "predicted_price_wan": None, "price_interval_wan": [], "capability_score": None,
            "conservative_capability_score": None, "protocol_score_interval": [],
            "feasibility_probability": None, "physical_gate": {"passed": None, "decision": "model_unavailable"},
            "cost_effectiveness": None, "params": dict(params), "tags": list(base.get("tags") or []),
            "tag_evidence": {}, "is_generated": True, "evaluation": None,
            "model_evaluation_available": False, "model_evaluation_error": str(error),
            "active_parameters": active_set["active_parameters"],
            "inactive_parameters": active_set["inactive_parameters"],
            "engineering_conflicts": [], "constraint_conflicts": [],
            "extrapolation_warnings": ["模型服务拒绝该外推输入；当前仅保留参数探索方案。"],
        }
        unmet, demand_penalty, assessment = self._demand_assessment(item, request, definitions, {})
        item.update({
            "unmet_conditions": unmet, "demand_unmet_conditions": unmet,
            "requirement_assessment": assessment, "strict_filter_satisfied": False,
            "best_effort": True, "generation_level": "exploratory",
            "fit_penalty": round(demand_penalty, 6), "recommendation_confidence": "low",
            "recommendation_warning": "模型评价不可用；该结果仅作为参数探索方案。",
            "generation_trace": {
                "method": "exploratory_parameter_fallback", "seed_agreement_id": base.get("agreement_id"),
                "seed_source": base.get("agreement_source") or base.get("seed_source") or "historical",
                "locked_parameters": sorted(locked), "search_move": search_move,
                "iteration": iteration, "attempt": attempt,
                "node_id": "X%03d-%03d" % (int(iteration), int(attempt)), "parent_node_id": None,
                "request_context": {
                    "selected_tags": list(request.get("selected_tags") or []),
                    "indicator_filter_mode": request.get("indicator_filter_mode", "all"),
                    "indicator_filters": list(request.get("indicator_filters") or []),
                },
                "model_evaluation_error": str(error),
            },
        })
        distance = self._normalized_distance(params, base.get("params") or {}, definitions)
        item["_search_key"] = (9.0, round(demand_penalty, 8), 0.0, 9.0, 9.0, 0.0, round(distance, 8))
        item["_risk_signature"] = ("model_evaluation_unavailable",)
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
        # Inactive subordinates never enter any move type; filter the adjustable
        # key list once so single/output/reverse/correlated moves all obey it.
        constraint_rules = getattr(self.store, "constraint_rows", lambda: [])()
        active_set = active_parameter_set(center, definitions, constraint_rules, locked=locked)
        active_keys = set(active_set["active_parameters"])
        compensation = [key for key in compensation if key in active_keys]
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

        # Two-variable coordinated moves use engineering coupling pairs by explicit
        # priority (DataMaster > learned coupling > conditional relationship) with
        # adjacent exploration pairs only as a fallback.
        pair_pool = build_coupling_pairs(
            active_set["active_parameters"], locked, definitions,
            datamaster_rows=getattr(self.store, "coupling_rows", lambda: [])(),
            learned_couplings=getattr(self.runtime.effectiveness, "couplings", []),
            conditional_rules=constraint_rules,
        )
        if not pair_pool:
            pair_pool = exploration_pairs(active_set["active_parameters"], locked, definitions, limit=5)
        for pair in pair_pool[:5]:
            a, b = pair["a"], pair["b"]
            if a not in center or b not in center or a not in stds or b not in stds:
                continue
            trial = dict(center)
            sign_a = -1.0 if (iteration + len(proposals)) % 2 else 1.0
            sign_b = -sign_a if pair.get("direction") == "opposite" else sign_a
            trial[a] = float(trial[a]) + sign_a * float(stds[a]) * step_scale
            trial[b] = float(trial[b]) + sign_b * float(stds[b]) * step_scale
            structural = pair["source"] == "conditional_relationship"
            label_prefix = "结构调整" if structural else "工程联动"
            proposals.append((trial, "%s：%s/%s" % (label_prefix, definitions[a].get("label", a), definitions[b].get("label", b))))

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

    def _finalize_params(self, params, base, locked, definitions, soft_strength=0.30, repair_reference=None):
        """Single candidate canonicalization pipeline.

        Order: restore locked -> conditional projection -> boundary/contour/coupling
        repair -> conditional projection again -> round.  The returned params are the
        exact values the model will evaluate, so signature/dedup/batch evaluation all
        share one canonical form.
        """
        self._restore_locked(params, locked)
        constraint_rules = getattr(self.store, "constraint_rows", lambda: [])()
        projection1 = project_constraints(params, definitions, constraint_rules, locked=locked, seed_values=base.get("params"))
        params = projection1["parameters"]
        boundary_repairs = self._repair_learned_boundaries(params, locked, definitions)
        soft_moves = self._soft_adjust_unlocked_contours(params, locked, definitions, strength=soft_strength)
        reference = repair_reference if repair_reference is not None else base["params"]
        repairs = self._repair_relations(params, reference, definitions, locked)
        repairs.extend(self._repair_learned_boundaries(params, locked, definitions))
        repairs.extend(boundary_repairs)
        # Second projection: coupling/boundary repair may have moved a controller or
        # subordinate; the conditional relationship must hold before evaluation.
        projection2 = project_constraints(params, definitions, constraint_rules, locked=locked, seed_values=base.get("params"))
        params = projection2["parameters"]
        repairs = list(dict.fromkeys(value for value in repairs if value))
        self._restore_locked(params, locked)
        self._round_values(params, definitions, locked)
        return {
            "params": params,
            "repairs": repairs,
            "soft_moves": soft_moves,
            "constraint_conflicts": projection2["conflicts"],
            "inactive_parameters": projection2["inactive_parameters"],
            "projection_repairs": projection1["repairs"] + projection2["repairs"],
        }

    def _changed_parameters(self, params, seed, definitions):
        changed = []
        for key, definition in definitions.items():
            if key not in params or key not in seed:
                continue
            if not self._value_equal(params[key], seed[key], definition):
                changed.append(key)
        return changed

    @staticmethod
    def _diversity_quality_eligible(item, best):
        """Keep diversity inside the same hard/requirement quality band."""
        key, best_key = item["_search_key"], best["_search_key"]
        if key[0] > best_key[0] + 1e-9:
            return False
        return key[1] <= best_key[1] + max(0.20, abs(best_key[1]) * 0.50 + 0.05)

    def _beam_select(self, records, definitions, width=10, branch_effort=None):
        records = sorted(records, key=lambda x: x["_search_key"])
        if not records:
            return []
        branch_effort = branch_effort or {}
        eligible = [item for item in records if self._diversity_quality_eligible(item, records[0])]
        selected, family_counts = [], {}
        family_cap = max(1, int(math.ceil(float(width) * 0.40)))

        def add(item, enforce_distance=True, enforce_family=True):
            if item in selected:
                return False
            family = item.get("family_id") or ""
            if enforce_family and family and family_counts.get(family, 0) >= family_cap:
                return False
            if enforce_distance and selected and min(
                self._normalized_distance(item["params"], other["params"], definitions) for other in selected
            ) < 0.008:
                return False
            selected.append(item)
            if family:
                family_counts[family] = family_counts.get(family, 0) + 1
            return True

        # Stage A: preserve one quality-qualified centre from every demand branch.
        branch_ids = []
        for item in eligible:
            branch_id = item.get("demand_branch_id") or "BRANCH-ALL"
            if branch_id not in branch_ids:
                branch_ids.append(branch_id)
        branch_order = dict((branch_id, index) for index, branch_id in enumerate(branch_ids))
        branch_ids.sort(key=lambda branch_id: (
            int((branch_effort.get(branch_id) or {}).get("round_opportunities", 0) or 0),
            int((branch_effort.get(branch_id) or {}).get("beam_admissions", 0) or 0),
            branch_order[branch_id],
        ))
        for branch_id in branch_ids:
            add(next(item for item in eligible if (item.get("demand_branch_id") or "BRANCH-ALL") == branch_id), enforce_distance=False)
            if len(selected) >= width:
                return selected[:width]
        # Stage B: preserve distinct seed families before global competition.
        seen_families = set()
        for item in eligible:
            family = item.get("family_id") or ""
            if family and family not in seen_families:
                add(item)
                seen_families.add(family)
            if len(selected) >= width:
                return selected[:width]
        # Stage C: fill globally, applying the soft 40% family cap while alternatives exist.
        for item in records:
            add(item)
            if len(selected) >= width:
                return selected[:width]
        for item in records:
            add(item, enforce_distance=False, enforce_family=False)
            if len(selected) >= width:
                break
        return selected

    def _update_branch_beam_effort(self, current_beam, comparison_pool, branch_ids, branch_effort, round_number):
        """Track quality eligibility separately from finite beam admission."""
        quality_counts = dict((branch_id, 0) for branch_id in branch_ids or [])
        selected_counts = dict((branch_id, 0) for branch_id in branch_ids or [])
        pool = list(comparison_pool or current_beam or [])
        best = min(pool, key=lambda item: item["_search_key"]) if pool else None
        for record in pool:
            branch_id = record.get("demand_branch_id")
            if branch_id in quality_counts and (best is None or self._diversity_quality_eligible(record, best)):
                quality_counts[branch_id] += 1
        for record in current_beam or []:
            branch_id = record.get("demand_branch_id")
            if branch_id in selected_counts:
                selected_counts[branch_id] += 1
        for branch_id in quality_counts:
            effort = branch_effort.setdefault(branch_id, {})
            effort["quality_eligible_centers"] = quality_counts[branch_id]
            effort["selected_beam_centers"] = selected_counts[branch_id]
            if selected_counts[branch_id]:
                effort["beam_admissions"] = int(effort.get("beam_admissions", 0) or 0) + 1
                effort["last_alive_round"] = round_number
        return branch_effort

    @staticmethod
    def _beam_candidate_pool(round_records, all_records, branch_ids):
        """Keep the best known centre from every branch eligible for rotation."""
        pool = list(round_records or [])
        present = set(id(item) for item in pool)
        for branch_id in branch_ids or []:
            candidates = [
                item for item in (all_records or [])
                if item.get("demand_branch_id") == branch_id
            ]
            if not candidates:
                continue
            best = min(candidates, key=lambda item: item["_search_key"])
            if id(best) not in present:
                pool.append(best)
                present.add(id(best))
        return pool

    def _diverse_strict_count(self, records, definitions, historical=None, distance=0.018, required_branch_ids=None):
        """Count strict solutions that can actually survive final diversity selection."""
        strict_records = [record for record in records if record.get("strict_filter_satisfied")]
        selected = []
        for item in sorted(
            strict_records,
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
        required = set(required_branch_ids or [])
        covered = set(item.get("demand_branch_id") for item in selected if item.get("demand_branch_id"))
        available_families = set(item.get("family_id") for item in strict_records if item.get("family_id"))
        covered_families = set(item.get("family_id") for item in selected if item.get("family_id"))
        family_coverage_ok = len(covered_families) >= min(2, len(available_families))
        return len(selected) if (not required or required.issubset(covered)) and family_coverage_ok else 0

    @staticmethod
    def _branch_search_states(records, branch_ids, completed_rounds, minimum_rounds=2, minimum_records=2, branch_effort=None):
        """Classify demand branches after each has received a minimum effort."""
        branch_effort = branch_effort or {}
        states = {}
        for branch_id in branch_ids or []:
            branch_records = [item for item in records if item.get("demand_branch_id") == branch_id]
            tracks_capacity = branch_id in branch_effort
            effort = branch_effort.get(branch_id) or {}
            branch_rounds = int(effort.get("round_opportunities", completed_rounds) or 0)
            seed_attempts = int(effort.get("seed_attempts", 0) or 0)
            quality_centers = int(effort.get("quality_eligible_centers", 0) or 0)
            selected_centers = int(effort.get("selected_beam_centers", 0) or 0)
            proposal_attempts = int(effort.get("proposal_attempts", 0) or 0)
            has_strict = any(item.get("strict_filter_satisfied") for item in branch_records)
            if tracks_capacity and quality_centers > 0 and selected_centers == 0:
                status = "waiting_for_capacity"
            elif tracks_capacity and quality_centers > 0 and branch_rounds == 0:
                status = "still_searching"
            elif has_strict:
                status = "strict_found"
            elif branch_records and seed_attempts and quality_centers == 0:
                status = "exhausted_by_quality"
            elif seed_attempts and not branch_records:
                status = "exhausted"
            elif branch_rounds >= minimum_rounds and len(branch_records) >= minimum_records:
                status = "best_effort_only"
            else:
                status = "still_searching"
            states[branch_id] = {
                "status": status, "evaluated_candidates": len(branch_records),
                "seed_attempts": seed_attempts, "round_opportunities": branch_rounds,
                "quality_eligible_centers": quality_centers,
                "selected_beam_centers": selected_centers,
                "beam_admissions": int(effort.get("beam_admissions", 0) or 0),
                "proposal_attempts": proposal_attempts,
                "last_alive_round": effort.get("last_alive_round"),
            }
        return states

    def _coverage_first_select(self, ranked, count, definitions, rejection=None):
        """Select quality-constrained branch/family coverage before global fill."""
        ranked = list(ranked or [])
        if not ranked:
            return []
        eligible = [item for item in ranked if self._diversity_quality_eligible(item, ranked[0])]
        selected, selected_risks = [], set()

        def add(item, enforce_distance=True):
            if item in selected:
                return False
            risk = item.get("_risk_signature")
            if risk and risk in selected_risks:
                if rejection is not None:
                    rejection["repeated_risk_signature"] += 1
                return False
            if enforce_distance and selected and min(
                self._normalized_distance(item["params"], existing["params"], definitions) for existing in selected
            ) < 0.018:
                return False
            selected.append(item)
            if risk:
                selected_risks.add(risk)
            return True

        seen = set()
        for item in eligible:
            branch = item.get("demand_branch_id") or "BRANCH-ALL"
            if branch not in seen:
                add(item, enforce_distance=False)
                seen.add(branch)
            if len(selected) >= count:
                return selected
        seen = set(item.get("family_id") for item in selected)
        for item in eligible:
            family = item.get("family_id")
            if family not in seen:
                add(item)
                seen.add(family)
            if len(selected) >= count:
                return selected
        for item in ranked:
            add(item)
            if len(selected) >= count:
                return selected
        for item in ranked:
            add(item, enforce_distance=False)
            if len(selected) >= count:
                break
        return selected

    def _build_generation_path(self, record, node_map):
        """Backtrace a candidate's node chain into a replayable generation path."""
        path = []
        seen = set()
        current = record
        while current is not None:
            trace = current.get("generation_trace") or {}
            node_id = trace.get("node_id")
            if not node_id or node_id in seen:
                break
            seen.add(node_id)
            path.append({
                "node_id": node_id,
                "iteration": trace.get("iteration"),
                "attempt": trace.get("attempt"),
                "move_type": trace.get("move_type"),
                "move": trace.get("move"),
                "params": dict(current.get("params") or {}),
            })
            parent_id = trace.get("parent_node_id")
            current = node_map.get(parent_id) if parent_id else None
        path.reverse()
        return path

    @staticmethod
    def _attach_lineage(record, base, branch_info):
        branch_info = dict(branch_info or {})
        demand_branch_id = branch_info.get("demand_branch_id") or "BRANCH-ALL"
        seed_id = str((base or {}).get("agreement_id") or "UNKNOWN-SEED")
        record["demand_branch_id"] = demand_branch_id
        record["seed_id"] = seed_id
        record["family_id"] = "%s:%s" % (demand_branch_id, seed_id)
        trace = record.setdefault("generation_trace", {})
        trace["generation_branch"] = branch_info
        trace["demand_branch_id"] = demand_branch_id
        trace["seed_id"] = seed_id
        trace["family_id"] = record["family_id"]
        return record

    def _make_public_item(self, record, base, changed, index, rng):
        item = dict(record)
        for private_key in ("_search_key", "_base", "_locked", "_anchor_conflicts", "_changed", "_request", "_risk_signature"):
            item.pop(private_key, None)
        trace = dict(item.get("generation_trace") or {})
        trace["changed_parameters"] = list(changed)
        item["generation_trace"] = trace
        branch = trace.get("generation_branch") or {}
        if branch.get("show_solution_direction"):
            item["solution_direction"] = {
                "branch_id": item.get("demand_branch_id") or branch.get("demand_branch_id") or "BRANCH-ALL",
                "title": branch.get("title") or "按当前综合需求探索",
                "summary": branch.get("summary") or "该方案代表当前综合需求下的一条参数调整路线。",
            }
        else:
            item.pop("solution_direction", None)
        item["agreement_id"] = "LIVE-%06d-%03d" % (rng.randint(0, 999999), index)
        item["agreement_name"] = "基于%s生成的候选协议-%02d" % (base["agreement_id"], index)
        if item.get("generation_level") == "exploratory":
            item["generation_note"] = "已按用户条件生成参数组合，但模型服务拒绝评价；当前结果仅供参数探索。"
            item["solution_fit_label"] = "探索参数方案"
            item["solution_fit_summary"] = "模型评价不可用，参数组合仍予保留，请人工复核后继续探索"
        elif item.get("best_effort"):
            item["generation_note"] = "根据用户条件、标签规则和模型输出目标进行迭代优化，并完成价格与效能联合评价。"
            item["solution_fit_label"] = "尽力方案"
            item["solution_fit_summary"] = "仍有需求或工程硬规则未满足，请结合风险提示继续调整"
        elif item.get("extrapolation_warnings"):
            item["generation_note"] = "根据用户条件、标签规则和模型输出目标进行迭代优化，并完成价格与效能联合评价。"
            item["solution_fit_label"] = "需求满足·外推评估"
            item["solution_fit_summary"] = "已满足筛选条件，但部分组合超出历史经验范围，预测可信度下降"
        else:
            item["generation_note"] = "根据用户条件、标签规则和模型输出目标进行迭代优化，并完成价格与效能联合评价。"
            item["solution_fit_label"] = "满足条件"
            item["solution_fit_summary"] = "已满足当前筛选条件且位于主要模型经验范围内"
        return item

    def _emergency_candidate(self, seeds, bounds, definitions, request, tag_map, rng, frozen_parameters=None, budget=None, rejection_details=None, anchor_filters=None):
        if budget is None:
            budget = {"attempted": 0, "max": 10 ** 9}
        for base in seeds:
            if budget["attempted"] >= budget["max"]:
                break
            params = dict(base.get("params") or {})
            locked, conflicts = self._anchor_demands(params, bounds, definitions)
            self._apply_frozen(params, locked, frozen_parameters)
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
            finalized = self._finalize_params(params, base, locked, definitions)
            anchor_violations = validate_anchor_integrity(
                finalized["params"], anchor_filters if anchor_filters is not None else request.get("indicator_filters"),
                definitions, "all" if anchor_filters is not None else request.get("indicator_filter_mode", "all")
            )
            if anchor_violations:
                if rejection_details is not None:
                    rejection_details.append({
                        "stage": "anchor_integrity",
                        "candidate_id": "EMERGENCY",
                        "error_type": "AnchorInvariantError",
                        "message": "；".join("%s %s actual=%s" % (v["parameter_id"], v["requested"], v["actual"]) for v in anchor_violations),
                    })
                continue
            repairs, soft = finalized["repairs"], finalized["soft_moves"]
            try:
                budget["attempted"] += 1
                record = self._record_from_params(finalized["params"], base, request, definitions, tag_map, locked, conflicts, repairs, soft, 0, 0, "兜底微调",
                                                  constraint_conflicts=finalized["constraint_conflicts"], inactive_parameters=finalized["inactive_parameters"], projection_repairs=finalized["projection_repairs"])
            except Exception:
                continue
            return record, base
        return None, None

    def _completion_value(self, key, item, history, definition, spec):
        """Complete an unspecified model field without touching user locks."""
        sources = []
        base_params = dict((item.get("base") or {}).get("params") or {})
        sources.append(("seed", base_params.get(key)))
        for historical in history or []:
            sources.append(("other_history", (historical.get("params") or {}).get(key)))
        sources.extend([
            ("business_default", definition.get("business_default")),
            ("business_default", definition.get("default_value")),
        ])
        allowed = self._normalized_allowed_values(definition)
        if allowed:
            sources.append(("business_allowed", allowed[0]))
        sources.extend([
            ("model_default", spec.get("default_value")),
            ("training_mean", spec.get("training_mean")),
        ])
        lower = spec.get("generation_min", spec.get("training_min", definition.get("min_value")))
        upper = spec.get("generation_max", spec.get("training_max", definition.get("max_value")))
        if lower is not None and upper is not None:
            try:
                sources.append(("reference_midpoint", (float(lower) + float(upper)) / 2.0))
            except (TypeError, ValueError):
                pass
        for source, value in sources:
            if value not in (None, ""):
                return value, source
        return None, None

    def _generation_input_preflight(self, pending_items, definitions, history=None):
        """Repair model-required fields and report remaining input risks.

        Returns ``None`` when the runtime/store does not expose schema metadata
        (e.g. unit tests with mocks). Preflight is diagnostic and repairable; it
        is never a generation-wide termination gate.
        """
        if not hasattr(self.runtime, "all_feature_specs") or not hasattr(self.store, "runtime_parameters"):
            return None
        try:
            specs = self.runtime.all_feature_specs()
        except Exception:
            return None
        required = {}
        for spec in specs or []:
            key = spec.get("key")
            if key and spec.get("required") and (spec.get("missing_policy") or "reject") == "reject":
                required[key] = spec
        if not required:
            return None
        seed_results = []
        missing_agg = {}
        unmapped_agg = {}

        def equivalent(left, right):
            if str(left).strip() == str(right).strip():
                return True
            left_num, right_num = normalize_numeric(left), normalize_numeric(right)
            return left_num is not None and right_num is not None and abs(left_num - right_num) < 1e-9

        def validate_business_input(business):
            """Rebuild model input and validate the complete contract after repair."""
            model_params = self.store.runtime_parameters(business)
            missing = [key for key in required if model_params.get(key) in (None, "")]
            invalid = []
            for key, spec in required.items():
                business_value = business.get(key)
                model_value = model_params.get(key)
                definition = definitions.get(key) or {}
                raw_mapping = definition.get("model_value_mapping_json")
                try:
                    mapping = json.loads(raw_mapping) if isinstance(raw_mapping, str) else (raw_mapping or {})
                except Exception:
                    mapping = {}
                if not isinstance(mapping, dict):
                    mapping = {}
                if mapping and business_value not in (None, ""):
                    known_business = any(equivalent(business_value, candidate) for candidate in mapping)
                    known_model = any(equivalent(business_value, candidate) for candidate in mapping.values())
                    if not known_business and not known_model:
                        invalid.append(key)
                        continue
                allowed = spec.get("allowed_values") or spec.get("categories") or []
                if allowed and model_value not in (None, ""):
                    if not any(equivalent(model_value, candidate) for candidate in allowed):
                        invalid.append(key)
            return model_params, missing, sorted(set(invalid))

        for item in pending_items:
            business = item.setdefault("params", {})
            locked = set(item.get("locked") or {})
            repairs = []
            for key, spec in required.items():
                if business.get(key) not in (None, "") or key in locked:
                    continue
                value, source = self._completion_value(key, item, history, definitions.get(key) or {}, spec)
                if value not in (None, ""):
                    business[key] = value
                    repairs.append({"parameter_id": key, "source": source})
            model_params, missing, unmapped = validate_business_input(business)
            for key in list(unmapped):
                if key in locked:
                    continue
                definition = definitions.get(key) or {}
                allowed = self._normalized_allowed_values(definition)
                mapping = definition.get("model_value_mapping_json")
                try:
                    mapping = json.loads(mapping) if isinstance(mapping, str) else (mapping or {})
                except Exception:
                    mapping = {}
                replacement = allowed[0] if allowed else (next(iter(mapping), None) if isinstance(mapping, dict) else None)
                if replacement not in (None, ""):
                    business[key] = replacement
                    repairs.append({"parameter_id": key, "source": "business_mapping"})
            if repairs:
                item.setdefault("completion_repairs", []).extend(repairs)
            # Never infer that an unlocked value was repaired. Re-run conversion,
            # required-field, mapping and model-domain validation from scratch.
            model_params, missing, unmapped = validate_business_input(business)
            for key in missing:
                missing_agg[key] = missing_agg.get(key, 0) + 1
            for key in set(unmapped):
                unmapped_agg.setdefault(key, []).append(str(business.get(key)))
            seed_results.append({
                "seed_id": (item.get("base") or {}).get("agreement_id"),
                "eligible": not missing and not unmapped,
                "missing_required_fields": missing,
                "unmapped_values": sorted(set(unmapped)),
                "completion_repairs": repairs,
            })
        return {
            "seed_count": len(pending_items),
            "eligible_seed_count": sum(1 for r in seed_results if r["eligible"]),
            "missing_required_fields": missing_agg,
            "unmapped_values": dict((k, sorted(set(v))) for k, v in unmapped_agg.items()),
            "seeds": seed_results,
        }

    def generate(self, request, count=5, seed=None, budget=1200, search_mode="fast", progress_callback=None):
        rng = random.Random(seed if seed is not None else int(time.time() * 1000) % 2147483647)
        deep_search = str(search_mode or "fast") in ("deep_extrapolation", "deep")
        definitions = self._parameter_definitions()
        explicit_feasibility = assess_explicit_filter_feasibility(
            request.get("indicator_filters"), definitions, request.get("indicator_filter_mode", "all")
        )
        target_protocol = request.get("target_protocol")
        try:
            history = self.store.historical_agreements(target_protocol=target_protocol)
        except TypeError:
            history = self.store.historical_agreements()
        expert_service = ExpertSchemeService(
            definitions, getattr(self.store, "current_product_code", lambda: "")(), self.runtime,
            encode_parameters=getattr(self.store, "runtime_parameters", None),
        )
        if hasattr(self.store, "list_saved"):
            raw_experts, signatures = [], set()
            for saved in self.store.list_saved():
                if not expert_service.compatibility(saved).get("recommendation_eligible_effective"):
                    continue
                signature = expert_service.canonical_parameter_signature(saved.get("params") or {})
                if signature in signatures:
                    continue
                if any(self._normalized_distance(
                    saved.get("params") or {}, prior.get("params") or {}, definitions,
                ) < 0.018 for prior in raw_experts):
                    continue
                signatures.add(signature)
                saved = dict(saved)
                saved.update({
                    "agreement_id": "SAVED-%s" % saved["id"],
                    "agreement_name": saved.get("scheme_name"),
                    "agreement_source": "expert_saved", "seed_source": "expert_saved",
                    "tags": self.store.derive_tags(saved.get("params") or {}, saved.get("evaluation") or {}),
                    "predicted_price_wan": (saved.get("evaluation") or {}).get("predicted_price_wan"),
                    "capability_score": (saved.get("evaluation") or {}).get("capability_score"),
                })
                raw_experts.append(saved)
            # Cheap requirement/distance screening happens on persisted business
            # snapshots before any HTTP model evaluation.
            potential_experts = self.select_seeds(
                request, min(32, len(raw_experts)), historical=raw_experts,
            ) if raw_experts else []
            pending = [{
                "candidate_id": item["agreement_id"], "parameters": item.get("params") or {},
                "base_parameters": item.get("base_params") or item.get("params") or {},
                "target_protocol": target_protocol,
            } for item in potential_experts]
            if pending:
                try:
                    evaluations = (
                        self.evaluate_batch_callback(pending) if self.evaluate_batch_callback else
                        [self.evaluate_callback(
                            item["parameters"], item["base_parameters"], target_protocol=item.get("target_protocol"),
                        ) for item in pending]
                    )
                except Exception:
                    evaluations = []
                for saved, evaluation in zip(potential_experts, evaluations):
                    expert = dict(saved)
                    expert["params"] = dict(evaluation.get("parameters") or saved.get("params") or {})
                    expert["evaluation"] = evaluation
                    for key in ("predicted_price_wan", "capability_score", "conservative_capability_score",
                                "feasibility_probability", "cost_effectiveness"):
                        expert[key] = evaluation.get(key)
                    expert["tags"] = self.store.derive_tags(expert["params"], evaluation)
                    history.append(expert)
        seed_count = max(2, min(int(request.get("seed_count") or 12), 40))
        seeds = self.select_seeds(request, min(seed_count, len(history)), historical=history)
        if not seeds:
            raise ValueError("没有历史协议可作为生成种子")
        numeric, _means, stds, lower = self._local_statistics(seeds, definitions)
        tag_map = self.store.tag_map()
        branch_bounds = []
        generation_branches = self._generation_branches(request, definitions)
        active_demand_branch_ids = sorted(set(
            branch.get("demand_branch_id") for branch in generation_branches if branch.get("demand_branch_id")
        ))
        show_solution_direction = len(active_demand_branch_ids) > 1
        branch_effort = dict((branch_id, {
            "seed_attempts": 0, "round_opportunities": 0, "quality_eligible_centers": 0,
            "selected_beam_centers": 0, "beam_admissions": 0,
            "proposal_attempts": 0, "last_alive_round": None,
        })
                             for branch_id in active_demand_branch_ids)

        def branch_effort_row(branch_id):
            return branch_effort.setdefault(branch_id, {
                "seed_attempts": 0, "round_opportunities": 0, "quality_eligible_centers": 0,
                "selected_beam_centers": 0, "beam_admissions": 0,
                "proposal_attempts": 0, "last_alive_round": None,
            })

        def update_beam_effort(current_beam, round_number, comparison_pool=None):
            self._update_branch_beam_effort(
                current_beam, comparison_pool, active_demand_branch_ids, branch_effort, round_number
            )
        tag_weights = dict((key, value.get("weight", 1.0)) for key, value in tag_map.items())
        all_records = []
        seen = set()
        rejection = {"not_changed": 0, "duplicate": 0, "model_input": 0, "hard_conflict": 0, "demand_unmet": 0, "extrapolation": 0, "known_boundary_repaired": 0, "repeated_risk_signature": 0, "conditional_frozen_conflict": 0, "anchor_invariant": 0}
        rejection_details = []
        evaluations = 0
        attempted_evaluations = 0
        max_evaluations = max(1, int(budget))
        time_budget_seconds = max(1, int(request.get("time_budget_seconds") or 35))
        deadline = time.monotonic() + time_budget_seconds
        stopped_for_time = False
        beam = []

        def record_rejection(stage, candidate_id, exc):
            if len(rejection_details) < 5:
                rejection_details.append({
                    "stage": stage,
                    "candidate_id": candidate_id,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                })

        # Stage 1: demand-anchored centres from every selected historical seed.
        # They are evaluated together.  Previous releases issued one price and one
        # effectiveness HTTP request per seed before the actual search started.
        initial_pending = []
        assignment_count = max(len(seeds), len(generation_branches))
        for seed_index in range(assignment_count):
            base = seeds[seed_index % len(seeds)]
            branch = generation_branches[seed_index % len(generation_branches)] if generation_branches else None
            bounds, branch_request, branch_info = self._compile_generation_branch(request, branch, definitions)
            branch_info["show_solution_direction"] = show_solution_direction
            branch_effort_row(branch_info["demand_branch_id"])["seed_attempts"] += 1
            branch_bounds.append(bounds)
            params = dict(base["params"])
            locked, anchor_conflicts = self._anchor_demands(params, bounds, definitions)
            anchor_resolutions = []
            for conflict in anchor_conflicts:
                if conflict.get("resolution") == "nearest_engineering_boundary":
                    anchor_resolutions.append({
                        "parameter_id": conflict["parameter_id"],
                        "requested": {
                            "min": conflict.get("requested_min"),
                            "max": conflict.get("requested_max"),
                        },
                        "resolved_value": conflict.get("resolved_value"),
                        "resolution": "nearest_engineering_boundary",
                        "strictly_satisfies_request": False,
                    })
            locked_sources = self._apply_frozen(params, locked, request.get("frozen_parameters"))
            for resolution in anchor_resolutions:
                locked_sources[resolution["parameter_id"]] = "engineering_boundary_fallback"
            finalized = self._finalize_params(params, base, locked, definitions, soft_strength=0.18)
            params = finalized["params"]
            anchor_violations = validate_anchor_integrity(
                params, branch_info.get("anchor_filters"), definitions, "all"
            )
            if anchor_violations:
                rejection["anchor_invariant"] += 1
                rejection_details.append({
                    "stage": "anchor_integrity",
                    "candidate_id": "SEED-%04d" % seed_index,
                    "error_type": "AnchorInvariantError",
                    "message": "；".join("%s %s actual=%s" % (v["parameter_id"], v["requested"], v["actual"]) for v in anchor_violations),
                })
                continue
            signature = tuple((key, params[key]) for key in sorted(params))
            if signature in seen:
                continue
            seen.add(signature)
            initial_pending.append({
                "params": params,
                "base": base,
                "request": branch_request,
                "locked": locked,
                "locked_sources": locked_sources,
                "anchor_conflicts": anchor_conflicts,
                "anchor_resolutions": anchor_resolutions,
                "repairs": finalized["repairs"],
                "soft_moves": finalized["soft_moves"],
                "constraint_conflicts": finalized["constraint_conflicts"],
                "inactive_parameters": finalized["inactive_parameters"],
                "projection_repairs": finalized["projection_repairs"],
                "branch_info": branch_info,
                "attempt": seed_index + 1,
            })

        # Candidate evaluation budget is a hard cap: never exceed it with the
        # initial demand-anchored seed batch.
        initial_pending = initial_pending[:max_evaluations]

        # Model-input preflight: reject seeds that would fail at evaluation time
        # before spending any generation budget.
        preflight = self._generation_input_preflight(initial_pending, definitions, history=history)

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
            except Exception as exc:
                record_rejection("batch_evaluation", "SEED-BATCH", exc)
                initial_evaluations = None

        for initial_index, item in enumerate(initial_pending):
            attempted_evaluations += 1
            try:
                evaluation = initial_evaluations[initial_index] if initial_evaluations is not None else None
                record = self._record_from_params(
                    item["params"], item["base"], item["request"], definitions, tag_map,
                    item["locked"], item["anchor_conflicts"], item["repairs"], item["soft_moves"],
                    0, item["attempt"], "需求与标签条件投影", evaluation=evaluation,
                    constraint_conflicts=item.get("constraint_conflicts"),
                    inactive_parameters=item.get("inactive_parameters"),
                    projection_repairs=item.get("projection_repairs"),
                )
            except Exception as exc:
                rejection["model_input"] += 1
                record_rejection("model_evaluation", "SEED-%04d" % initial_index, exc)
                record = self._exploratory_record(
                    item["params"], item["base"], item["request"], definitions,
                    item["locked"], 0, item["attempt"], "需求与标签条件投影", exc,
                )
            else:
                evaluations += 1
            record["_base"] = item["base"]
            record["_locked"] = item["locked"]
            record["_locked_sources"] = item["locked_sources"]
            record["_anchor_conflicts"] = item["anchor_conflicts"]
            record["_request"] = item["request"]
            record["generation_trace"]["tag_branch"] = item["branch_info"]
            record["generation_trace"]["locked_sources"] = item["locked_sources"]
            record["generation_trace"]["anchor_resolutions"] = item.get("anchor_resolutions") or []
            record["generation_trace"]["completion_repairs"] = item.get("completion_repairs") or []
            self._attach_lineage(record, item["base"], item["branch_info"])
            all_records.append(record)
            beam.append(record)
        beam_width = max(2, min(int(request.get("beam_width") or (14 if deep_search else 10)), 40))
        initial_beam_pool = list(beam)
        beam = self._beam_select(
            beam, definitions, width=min(beam_width, max(4, len(beam))), branch_effort=branch_effort
        )
        update_beam_effort(beam, 0, initial_beam_pool)

        # Stage 2: adaptive iterative neighborhoods. Better candidates become the
        # next centres; the step size shrinks as the search progresses.
        max_rounds = int(request.get("generation_rounds") or (7 if deep_search else 6))
        step_schedule = build_step_schedule("deep" if deep_search else "fast", max_rounds)
        iteration = 0
        diverse_strict = 0
        stopped_for_count = False
        pending = []
        while beam and attempted_evaluations < max_evaluations and iteration < len(step_schedule):
            if time.monotonic() >= deadline:
                stopped_for_time = True
                break
            iteration += 1
            for branch_id in active_demand_branch_ids:
                if branch_effort_row(branch_id).get("selected_beam_centers", 0) > 0:
                    branch_effort_row(branch_id)["round_opportunities"] += 1
            step_scale = step_schedule[iteration - 1]
            round_records = list(beam)
            pending = []
            remaining_rounds = len(step_schedule) - iteration + 1
            remaining_budget = max_evaluations - attempted_evaluations
            # Retain budget for cumulative multi-round movement in both modes.
            # A single first-round coordinate scan must not consume the whole
            # budget and degrade the search into one shallow neighborhood.
            if deep_search:
                round_budget = (
                    min(120, remaining_budget)
                    if iteration == 1 else
                    max(24, int(math.ceil(float(remaining_budget) / remaining_rounds)))
                )
            elif iteration == 1:
                round_budget = min(max(24, int(max_evaluations * 0.35)), remaining_budget)
            else:
                round_budget = max(16, int(math.ceil(float(remaining_budget) / remaining_rounds)))
            round_budget = max(1, min(round_budget, remaining_budget))
            proposals_by_center = []
            proposed_signatures = set()
            for center_record in beam:
                base = center_record["_base"]
                locked = center_record["_locked"]
                locked_sources = center_record.get("_locked_sources") or {}
                anchor_conflicts = center_record["_anchor_conflicts"]
                center_request = center_record.get("_request") or request
                compensation = self._compensation_keys(locked, definitions, numeric)
                proposals = self._neighborhood_params(
                    center_record["params"], base, locked, compensation, definitions,
                    numeric, stds, lower, step_scale, rng, iteration, center_request,
                )
                center_pending = []
                for proposal_index, (params, move_label) in enumerate(proposals):
                    finalized = self._finalize_params(params, base, locked, definitions, soft_strength=0.32, repair_reference=center_record["params"])
                    params = finalized["params"]
                    anchor_violations = validate_anchor_integrity(
                        params, (center_record.get("generation_trace", {}).get("generation_branch") or {}).get("anchor_filters", center_request.get("indicator_filters")), definitions, "all"
                    )
                    if anchor_violations:
                        rejection["anchor_invariant"] += 1
                        continue
                    changed = self._changed_parameters(params, base["params"], definitions)
                    if len(changed) < 1:
                        rejection["not_changed"] += 1
                        continue
                    # A frozen subordinate that a conditional controller forces
                    # inactive is a hard conflict: reject the proposal outright.
                    if finalized["constraint_conflicts"]:
                        rejection["conditional_frozen_conflict"] = rejection.get("conditional_frozen_conflict", 0) + 1
                        continue
                    signature = tuple((key, params[key]) for key in sorted(params))
                    if signature in seen or signature in proposed_signatures:
                        rejection["duplicate"] += 1
                        continue
                    proposed_signatures.add(signature)
                    center_pending.append({
                        "params": params, "base": base, "request": center_request,
                        "locked": locked, "locked_sources": locked_sources, "anchor_conflicts": anchor_conflicts,
                        "anchor_resolutions": center_record.get("generation_trace", {}).get("anchor_resolutions") or [],
                        "repairs": finalized["repairs"], "soft_moves": finalized["soft_moves"],
                        "constraint_conflicts": finalized["constraint_conflicts"],
                        "inactive_parameters": finalized["inactive_parameters"],
                        "projection_repairs": finalized["projection_repairs"],
                        "move_label": move_label, "center_record": center_record,
                        "signature": signature,
                    })
                if center_pending:
                    proposals_by_center.append(center_pending)
            if deep_search and iteration >= 2:
                for params, center_record, move_label in self._deep_extrapolation_moves(beam, definitions):
                    base = center_record["_base"]
                    locked = center_record["_locked"]
                    finalized = self._finalize_params(
                        params, base, locked, definitions, soft_strength=0.24,
                        repair_reference=center_record["params"],
                    )
                    params = finalized["params"]
                    anchor_violations = validate_anchor_integrity(
                        params, (center_record.get("generation_trace", {}).get("generation_branch") or {}).get("anchor_filters", (center_record.get("_request") or request).get("indicator_filters")), definitions, "all"
                    )
                    if anchor_violations:
                        rejection["anchor_invariant"] += 1
                        continue
                    if finalized["constraint_conflicts"]:
                        rejection["conditional_frozen_conflict"] = rejection.get("conditional_frozen_conflict", 0) + 1
                        continue
                    signature = tuple((key, params[key]) for key in sorted(params))
                    if signature in seen or signature in proposed_signatures:
                        continue
                    proposed_signatures.add(signature)
                    proposals_by_center.insert(0, [{
                        "params": params, "base": base,
                        "request": center_record.get("_request") or request,
                        "locked": locked,
                        "locked_sources": center_record.get("_locked_sources") or {},
                        "anchor_conflicts": center_record["_anchor_conflicts"],
                        "anchor_resolutions": center_record.get("generation_trace", {}).get("anchor_resolutions") or [],
                        "repairs": finalized["repairs"], "soft_moves": finalized["soft_moves"],
                        "constraint_conflicts": finalized["constraint_conflicts"],
                        "inactive_parameters": finalized["inactive_parameters"],
                        "projection_repairs": finalized["projection_repairs"],
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
                branch_id = item.get("center_record", {}).get("demand_branch_id")
                if branch_id:
                    branch_effort_row(branch_id)["proposal_attempts"] += 1
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
                except Exception as exc:
                    # Keep the old candidate-level failure semantics if a remote
                    # batch cannot be processed as a whole.
                    record_rejection("batch_evaluation", "SEARCH-BATCH-%02d" % iteration, exc)
                    batch_evaluations = None
            for pending_index, item in enumerate(pending):
                if attempted_evaluations >= max_evaluations:
                    break
                attempted_evaluations += 1
                try:
                    evaluation = batch_evaluations[pending_index] if batch_evaluations is not None else None
                    record = self._record_from_params(
                        item["params"], item["base"], item["request"], definitions, tag_map,
                        item["locked"], item["anchor_conflicts"], item["repairs"], item["soft_moves"],
                        iteration, evaluations + 1, item["move_label"], evaluation=evaluation,
                        parent_record=item.get("center_record"),
                        constraint_conflicts=item.get("constraint_conflicts"),
                        inactive_parameters=item.get("inactive_parameters"),
                        projection_repairs=item.get("projection_repairs"),
                    )
                except Exception as exc:
                    rejection["model_input"] += 1
                    record_rejection("model_evaluation", "SEARCH-%02d-%04d" % (iteration, pending_index), exc)
                    record = self._exploratory_record(
                        item["params"], item["base"], item["request"], definitions,
                        item["locked"], iteration, pending_index + 1, item["move_label"], exc,
                    )
                else:
                    evaluations += 1
                record["_base"] = item["base"]
                record["_locked"] = item["locked"]
                record["_locked_sources"] = item.get("locked_sources") or {}
                record["_anchor_conflicts"] = item["anchor_conflicts"]
                record["_request"] = item["request"]
                record["generation_trace"]["tag_branch"] = dict(item["center_record"].get("generation_trace", {}).get("tag_branch") or {})
                record["generation_trace"]["locked_sources"] = record["_locked_sources"]
                record["generation_trace"]["anchor_resolutions"] = item.get("anchor_resolutions") or []
                center_lineage = dict(item["center_record"].get("generation_trace", {}).get("generation_branch") or {})
                self._attach_lineage(record, item["base"], center_lineage)
                round_records.append(record)
                all_records.append(record)
            beam_pool = self._beam_candidate_pool(round_records, all_records, active_demand_branch_ids)
            beam = self._beam_select(
                beam_pool, definitions, width=beam_width, branch_effort=branch_effort
            )
            update_beam_effort(beam, iteration, beam_pool)
            # Stop as soon as final selection can return the requested number of
            # genuinely different strict solutions.  Counting three times the
            # requested amount caused unnecessary model batches on easy demands.
            branch_search_states = self._branch_search_states(
                all_records, active_demand_branch_ids, iteration, branch_effort=branch_effort
            )
            strict_capable_branches = [
                branch_id for branch_id, state in branch_search_states.items()
                if state["status"] == "strict_found"
            ]
            all_branches_explored = all(
                state["status"] not in ("still_searching", "waiting_for_capacity")
                for state in branch_search_states.values()
            )
            diverse_strict = self._diverse_strict_count(
                all_records, definitions, historical=history, required_branch_ids=strict_capable_branches
            )
            if diverse_strict >= count and all_branches_explored:
                stopped_for_count = True
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

        if not usable and attempted_evaluations < max_evaluations:
            budget_state = {"attempted": attempted_evaluations, "max": max_evaluations}
            emergency_records = []
            for branch in generation_branches:
                if budget_state["attempted"] >= budget_state["max"]:
                    break
                emergency_bounds, emergency_request, emergency_branch_info = self._compile_generation_branch(
                    request, branch, definitions
                )
                emergency_branch_info["show_solution_direction"] = show_solution_direction
                emergency, emergency_base = self._emergency_candidate(
                    seeds, emergency_bounds, definitions, emergency_request, tag_map, rng,
                    frozen_parameters=request.get("frozen_parameters"), budget=budget_state,
                    rejection_details=rejection_details,
                    anchor_filters=emergency_branch_info.get("anchor_filters"),
                )
                if emergency is None:
                    continue
                emergency["_base"] = emergency_base
                emergency["_changed"] = self._changed_parameters(emergency["params"], emergency_base["params"], definitions)
                self._attach_lineage(emergency, emergency_base, emergency_branch_info)
                if emergency["_changed"]:
                    emergency_records.append(emergency)
            attempted_evaluations = budget_state["attempted"]
            usable.extend(self._coverage_first_select(
                sorted(emergency_records, key=lambda item: item["_search_key"]), count, definitions
            ))

        if not usable and all_records:
            # Last-resort no-empty guarantee: return the closest successfully
            # evaluated record, clearly flagged as a fallback exploration result,
            # even when it duplicates history or has no changed parameter.  Its
            # true strict/best-effort status is preserved — a duplicate-only
            # fallback can still be a genuine strict solution.
            closest = min(all_records, key=lambda record: record["_search_key"])
            closest_base = closest.get("_base") or seeds[0]
            changed = self._changed_parameters(closest["params"], closest_base["params"], definitions)
            is_duplicate = any(
                self._normalized_distance(closest["params"], historical["params"], definitions) < 0.002
                for historical in history
            )
            if not changed:
                fallback_reason = "no_changed_parameter"
            elif is_duplicate:
                fallback_reason = "duplicate_only"
            else:
                fallback_reason = "no_usable_best_effort"
            closest["_base"] = closest_base
            closest["_changed"] = changed
            closest["fallback_kind"] = "closest_evaluated_result"
            closest["fallback_reason"] = fallback_reason
            usable.append(closest)

        strict_candidates = [item for item in usable if item.get("strict_filter_satisfied")]
        exploratory_candidates = [item for item in usable if item.get("generation_level") == "exploratory"]
        strict_available = bool(strict_candidates)
        if strict_available:
            ranking_pool = []
            for item in strict_candidates:
                public = dict(item)
                for key in ("_base", "_locked", "_anchor_conflicts", "_changed", "_search_key", "_request", "_risk_signature"):
                    public.pop(key, None)
                ranking_pool.append(public)
            ranked_public = rank_agreements(
                ranking_pool, request, tag_weights,
                definitions=definitions, tag_map=tag_map,
                constraint_rules=getattr(self.store, "constraint_rows", lambda: [])(),
            )
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

        selected = self._coverage_first_select(ranked, count, definitions, rejection=rejection)
        if not selected and usable:
            selected = usable[:1]

        node_map = {}
        for rec in all_records:
            node_id = (rec.get("generation_trace") or {}).get("node_id")
            if node_id:
                node_map[node_id] = rec
        public_selected = []
        for index, record in enumerate(selected, 1):
            record.setdefault("generation_trace", {})["generation_path"] = self._build_generation_path(record, node_map)
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

        if stopped_for_count:
            stopping_reason = "requested_count_met"
        elif stopped_for_time:
            stopping_reason = "time_budget_exhausted"
        elif attempted_evaluations >= max_evaluations:
            stopping_reason = "budget_exhausted"
        elif iteration >= len(step_schedule):
            stopping_reason = "max_rounds_reached"
        elif not beam:
            stopping_reason = "no_search_centers"
        elif not pending:
            stopping_reason = "no_more_proposals"
        else:
            stopping_reason = "search_completed"

        return {
            "candidates": public_selected,
            "requested_count": count,
            "evaluated_count": evaluations,
            "all_records_count": len(all_records),
            "usable_count": len(usable),
            "best_effort_candidate_count": len(usable) - len(strict_candidates),
            "exploratory_candidate_count": len(exploratory_candidates),
            "final_selected_count": len(public_selected),
            "fallback_used": any(item.get("fallback_kind") == "closest_evaluated_result" for item in public_selected),
            "seed_agreements": [item["agreement_id"] for item in seeds],
            "rejection_statistics": rejection,
            "rejection_details": rejection_details,
            "preflight": preflight,
            "generation_budget": max_evaluations,
            "time_budget_seconds": time_budget_seconds,
            "actual_budget_used": attempted_evaluations,
            "max_rounds": max_rounds,
            "actual_rounds": iteration,
            "stopping_reason": stopping_reason,
            "strict_filter_satisfied": strict_available,
            "strict_candidate_count": len(strict_candidates),
            "best_effort_used": not strict_available,
            "bounds": branch_bounds,
            "relaxation_suggestions": suggestions,
            "generation_method": (
                "deep_extrapolation_multistage_beam_search" if str(search_mode) == "deep_extrapolation"
                else "deep_diversity_beam_search" if deep_search
                else "batched_directional_beam_search"
            ),
            "search_iterations": iteration,
            "search_mode": str(search_mode or "fast"),
            "explicit_filter_feasibility": explicit_feasibility,
            "branch_search_states": self._branch_search_states(
                all_records, active_demand_branch_ids, iteration, branch_effort=branch_effort
            ),
        }
