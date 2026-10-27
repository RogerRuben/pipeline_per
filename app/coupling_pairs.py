# -*- coding: utf-8 -*-
"""Engineering coupling pair pool with explicit source priority (Phase 5C).

Multi-attribute moves are chosen from real engineering relationships instead of
adjacent list order.  Priority order::

    1. DataMaster indicator_couplings
    2. Effectiveness model learned couplings
    3. Conditional-attribute relationship (controller <-> subordinate)
    4. Fallback exploration pairs

Locked or inactive parameters never enter a pair.
"""
from __future__ import print_function

from .conditional_constraint import parse_template_metadata


def _eligible(key, active, locked, definitions):
    return (
        key and key in active and key not in locked
        and (definitions or {}).get(key, {}).get("auto_adjustable", 1)
    )


def build_coupling_pairs(active, locked, definitions,
                         datamaster_rows=None, learned_couplings=None, conditional_rules=None):
    """Return a deduplicated, priority-ordered pair list.

    Each pair is ``{"a", "b", "source", "priority", "strength"}``.
    """
    active = set(active or [])
    locked = set(locked or [])
    pairs = []
    seen = set()

    def add(a, b, source, priority, strength, relation_type="same_direction", direction="same"):
        if a == b:
            return
        key = tuple(sorted((a, b)))
        if key in seen:
            return
        seen.add(key)
        pairs.append({
            "a": a, "b": b, "source": source, "priority": priority,
            "strength": float(strength or 1.0), "relation_type": relation_type, "direction": direction,
        })

    # Priority 1: DataMaster indicator_couplings.
    for row in (datamaster_rows or []):
        a, b = row.get("parameter_a"), row.get("parameter_b")
        coupling_type = str(row.get("coupling_type") or "positive")
        relation_type = {
            "positive": "same_direction",
            "negative": "opposite_direction",
            "feasible_domain": "toward_feasible_boundary",
        }.get(coupling_type, "same_direction")
        direction = "opposite" if relation_type == "opposite_direction" else "same"
        if _eligible(a, active, locked, definitions) and _eligible(b, active, locked, definitions):
            add(a, b, "datamaster_coupling", 1, row.get("strength") or 1.0, relation_type, direction)

    # Priority 2: effectiveness model learned couplings.
    for model in (learned_couplings or []):
        target = model.get("target")
        for source in model.get("sources") or []:
            key = source.get("key") if isinstance(source, dict) else source
            strength = 1.0
            if isinstance(source, dict):
                strength = source.get("weight") or source.get("strength") or 1.0
            direction = "opposite" if float(strength) < 0 else "same"
            if _eligible(key, active, locked, definitions) and _eligible(target, active, locked, definitions):
                add(key, target, "learned_coupling", 2, strength, "same_direction" if direction == "same" else "opposite_direction", direction)

    # Priority 3: conditional controller <-> subordinate.
    for rule in (conditional_rules or []):
        if rule.get("rule_kind") not in ("conditional_lower", "conditional_upper"):
            continue
        meta = parse_template_metadata(rule)
        if not meta:
            continue
        controller, target = meta.get("controller"), meta.get("target")
        if _eligible(controller, active, locked, definitions) and _eligible(target, active, locked, definitions):
            add(controller, target, "conditional_relationship", 3, 1.0, "conditional_controller", "same")

    pairs.sort(key=lambda pair: (pair["priority"], -pair["strength"], pair["a"], pair["b"]))
    return pairs


def exploration_pairs(active, locked, definitions, limit=5):
    """Priority-4 fallback: adjacent exploration pairs for the remaining active set."""
    keys = [key for key in (active or []) if key not in set(locked or [])
            and (definitions or {}).get(key, {}).get("auto_adjustable", 1)]
    result = []
    for index in range(min(limit, max(len(keys) - 1, 0))):
        result.append({"a": keys[index], "b": keys[index + 1], "source": "exploration", "priority": 4, "strength": 0.0})
    return result
