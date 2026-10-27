# -*- coding: utf-8 -*-
"""Compile explicit and tag requirements into bounded generation branches."""

import heapq
import itertools


def _operator_text(operator):
    return {
        "gte": "不低于", "gt": "高于", "lte": "不高于", "lt": "低于",
        "eq": "等于", "boolean_is": "为", "special_is": "状态为",
        "text_equals": "等于", "text_contains": "包含", "range_inside": "位于区间",
    }.get(operator, str(operator or ""))


def _filter_label(rule, definitions):
    key = str(rule.get("parameter_id") or "")
    definition = (definitions or {}).get(key) or {}
    label = definition.get("label") or key or "技术条件"
    value = rule.get("value1")
    if str(rule.get("operator") or "").startswith("range_"):
        value = "%s～%s" % (value, rule.get("value2"))
    return "%s%s%s" % (label, _operator_text(rule.get("operator")), value)


def compile_explicit_demand_branches(filters, mode="all", definitions=None):
    filters = [dict(rule) for rule in (filters or []) if rule.get("parameter_id")]
    if str(mode or "all") != "any" or len(filters) <= 1:
        label = "联合满足当前技术要求" if filters else "按当前综合需求探索"
        return [{"explicit_branch_id": "BRANCH-ALL", "explicit_filters": filters,
                 "explicit_filter_mode": "all", "title": label, "summary": label}]
    return [
        {"explicit_branch_id": "BRANCH-%02d" % (index + 1), "explicit_filters": [rule],
         "explicit_filter_mode": "all", "title": "优先满足%s" % _filter_label(rule, definitions),
         "summary": "该方向围绕“%s”进行参数探索。" % _filter_label(rule, definitions)}
        for index, rule in enumerate(filters)
    ]


def _bounded_maximal_valid_sets(keys, conflict_groups, limit):
    """Return large valid key sets first without enumerating the full power set.

    A valid selected set must omit at least one key from every conflict group.
    The complement is therefore a hitting set.  A bounded best-first search over
    hitting sets yields the least-relaxed (largest selected) directions first.
    Greedy maximal sets provide deterministic coverage if pathological metadata
    exhausts the search-state budget before enough solutions are found.
    """
    ordered_keys = tuple(sorted(set(keys or [])))
    groups = []
    for group in conflict_groups or []:
        normalized = frozenset(str(key) for key in group if key in ordered_keys)
        if len(normalized) > 1 and normalized not in groups:
            groups.append(normalized)
    if not ordered_keys or not groups:
        return [set(ordered_keys)]

    cap = max(1, int(limit or 1))
    state_cap = max(4096, cap * 512)
    queue = [(0, tuple(), frozenset())]
    visited = {frozenset()}
    selected_sets = []
    selected_signatures = set()
    expanded = 0

    def add_selected(selected):
        signature = tuple(sorted(selected))
        if signature not in selected_signatures:
            selected_signatures.add(signature)
            selected_sets.append(set(selected))

    while queue and expanded < state_cap and len(selected_sets) < cap:
        _size, _ordered_drop, dropped = heapq.heappop(queue)
        expanded += 1
        uncovered = next((group for group in groups if not group.intersection(dropped)), None)
        if uncovered is None:
            # Superset hitting sets do not describe maximal selected sets.
            if any(all(group.intersection(dropped - {key}) for group in groups) for key in dropped):
                continue
            add_selected(set(ordered_keys) - set(dropped))
            continue
        for key in sorted(uncovered):
            next_dropped = frozenset(set(dropped) | {key})
            if next_dropped in visited:
                continue
            visited.add(next_dropped)
            heapq.heappush(queue, (len(next_dropped), tuple(sorted(next_dropped)), next_dropped))

    # Bounded greedy completion avoids the old >12 fallback that retained only
    # one core key.  Different rotations expose alternative maximal directions.
    orders = []
    for offset in range(len(ordered_keys)):
        rotated = ordered_keys[offset:] + ordered_keys[:offset]
        orders.extend((rotated, tuple(reversed(rotated))))
    for order in orders:
        selected = set()
        for key in order:
            trial = selected | {key}
            if not any(group.issubset(trial) for group in groups):
                selected = trial
        add_selected(selected)

    selected_sets.sort(key=lambda item: (-len(item), tuple(sorted(item))))
    return selected_sets[:cap]


def compile_conflict_core_branches(filters, conflict_key_groups, definitions=None, max_branches=24):
    """Relax only mutually incompatible filter keys and preserve common filters.

    Cross-field conflict groups are treated as hyperedges: a selected key set is
    valid when it does not contain every key in any edge.  Maximal valid sets are
    retained, so A↔B plus C↔D yields A+C, A+D, B+C and B+D while unrelated E is
    present in every branch.  A singleton group denotes contradictory filters on
    the same key; its individual rules become bounded alternatives.
    """
    filters = [dict(rule) for rule in (filters or []) if rule.get("parameter_id")]
    groups = [set(str(key) for key in group if key) for group in (conflict_key_groups or [])]
    groups = [group for group in groups if group]
    if not groups:
        return compile_explicit_demand_branches(filters, "all", definitions)
    core_keys = sorted(set().union(*groups))
    common = [rule for rule in filters if str(rule.get("parameter_id")) not in core_keys]
    singleton_keys = set(next(iter(group)) for group in groups if len(group) == 1)
    cross_groups = [group for group in groups if len(group) > 1]
    cross_keys = sorted(set().union(*cross_groups)) if cross_groups else []

    branch_cap = max(1, int(max_branches))
    valid_sets = []
    if cross_keys:
        valid_sets = _bounded_maximal_valid_sets(cross_keys, cross_groups, branch_cap)
    else:
        valid_sets = [set()]

    candidates = []
    signatures = set()
    for selected_keys in valid_sets:
        selected_cross = [
            rule for rule in filters
            if str(rule.get("parameter_id")) in selected_keys
            and str(rule.get("parameter_id")) not in singleton_keys
        ]
        active_singletons = sorted(
            key for key in singleton_keys if key not in cross_keys or key in selected_keys
        )
        singleton_choices = [
            [[rule] for rule in filters if str(rule.get("parameter_id")) == key]
            for key in active_singletons
        ]
        singleton_products = list(itertools.product(*singleton_choices)) if singleton_choices else [tuple()]
        for singleton_product in singleton_products:
            chosen = common + selected_cross + [rule for choice in singleton_product for rule in choice]
            signature = tuple(
                (rule.get("parameter_id"), rule.get("operator"), str(rule.get("value1")), str(rule.get("value2")))
                for rule in chosen
            )
            if signature not in signatures:
                signatures.add(signature)
                candidates.append((signature, chosen))
            if len(candidates) >= branch_cap:
                break
        if len(candidates) >= branch_cap:
            break

    result = []
    for index, (_signature, chosen) in enumerate(candidates):
        core_chosen = [rule for rule in chosen if str(rule.get("parameter_id")) in core_keys]
        direction = "、".join(_filter_label(rule, definitions) for rule in core_chosen) or "保留公共条件"
        result.append({
            "explicit_branch_id": "CONFLICT-%02d" % (index + 1),
            "explicit_filters": chosen, "assessment_filters": filters,
            "explicit_filter_mode": "all", "title": "冲突方向：%s" % direction,
            "summary": "保留所有无冲突条件，并优先满足“%s”；其它联合条件仍按未满足项如实标记。" % direction,
        })
    return result or compile_explicit_demand_branches(filters, "all", definitions)


def combine_generation_branches(explicit_branches, tag_branches, max_branches=24):
    explicit = list(explicit_branches or [])
    tags = list(tag_branches or []) or [{"rules": [], "tag_groups": {}, "unresolved_tags": []}]
    result = []
    # Tag-major round robin guarantees every explicit OR branch receives a slot
    # before secondary tag alternatives consume the bounded branch budget.
    for tag_index, tag in enumerate(tags):
        for demand in explicit:
            if len(result) >= max(1, int(max_branches)):
                return result
            branch_id = demand["explicit_branch_id"]
            if len(tags) > 1:
                branch_id += "-TAG-%02d" % (tag_index + 1)
            result.append({
                "branch_id": branch_id,
                "demand_branch_id": demand["explicit_branch_id"],
                "title": demand["title"], "summary": demand["summary"],
                "explicit_filters": list(demand["explicit_filters"]),
                "assessment_filters": list(demand.get("assessment_filters") or []),
                "explicit_conflicts": list(demand.get("explicit_conflicts") or []),
                "explicit_filter_mode": demand["explicit_filter_mode"],
                "tag_rules": list(tag.get("rules") or []),
                "tag_groups": dict(tag.get("tag_groups") or {}),
                "unresolved_tags": list(tag.get("unresolved_tags") or []),
            })
    return result
