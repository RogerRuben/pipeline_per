# -*- coding: utf-8 -*-
"""Conditional-attribute constraint template compiler (Phase 5A).

A binary/finite-state *controller* attribute decides whether a numeric
*subordinate* attribute is applicable.  When the controller is inactive the
subordinate collapses to ``inactive_value`` (e.g. -1); when active it takes its
normal engineering range ``[active_min, active_max]``.

The template is compiled into two ordinary affine ``constraint_rules`` rows so
the existing post-check and (Phase 5B) projection machinery consume them without
a parallel rule engine.  For a binary controller ``A`` (active when ``A=1``) and
subordinate ``B`` with inactive value ``C`` and active range ``[L, U]``::

    B >= (L - C) * A + C      -> conditional_lower
    B <= (U - C) * A + C      -> conditional_upper

Example C=-1, L=0, U=30 gives ``B >= A - 1`` and ``B <= 31A - 1``, i.e.
``A=0 -> B=-1`` and ``A=1 -> B in [0, 30]``.
"""
from __future__ import print_function

import json
import uuid

from .value_semantics import normalize_numeric

TEMPLATE_KIND = "conditional_numeric_applicability"
TEMPLATE_KIND_V2 = "conditional_applicability_v2"


def compile_conditional_constraint(controller, active_value, target,
                                   inactive_value, active_min, active_max,
                                   group_prefix="cond"):
    """Compile a template into ``{constraint_group, template_metadata, rules}``.

    ``rules`` contains exactly two affine rows (conditional_lower /
    conditional_upper) sharing the same ``constraint_group`` and
    ``template_metadata_json`` so admin edit/delete stay atomic.
    """
    active_min = float(active_min)
    active_max = float(active_max)
    if active_min > active_max:
        active_min, active_max = active_max, active_min
    inactive_value = float(inactive_value)
    group = "%s_%s_%s" % (group_prefix, str(controller), str(target))
    template_metadata = {
        "template": TEMPLATE_KIND,
        "controller": controller,
        "active_value": active_value,
        "target": target,
        "inactive_value": inactive_value,
        "active_min": active_min,
        "active_max": active_max,
    }
    lower_mult = active_min - inactive_value
    upper_mult = active_max - inactive_value
    lower = {
        "rule_kind": "conditional_lower",
        "constraint_group": group,
        "left_parameter": target,
        "operator": "gte",
        "right_parameter": controller,
        "multiplier": lower_mult,
        "offset": inactive_value,
        "template_metadata_json": json.dumps(template_metadata, ensure_ascii=False),
    }
    upper = {
        "rule_kind": "conditional_upper",
        "constraint_group": group,
        "left_parameter": target,
        "operator": "lte",
        "right_parameter": controller,
        "multiplier": upper_mult,
        "offset": inactive_value,
        "template_metadata_json": json.dumps(template_metadata, ensure_ascii=False),
    }
    return {"constraint_group": group, "template_metadata": template_metadata, "rules": [lower, upper]}


def compile_conditional_relationship_v2(controller, when, target, then, otherwise, group_prefix="cond"):
    """Compile a V2 business-facing conditional relationship.

    ``when`` describes the controller state that triggers ``then``; ``otherwise``
    is the active/fallback branch.  The metadata is the source of truth; the two
    affine rule rows are retained only so existing group delete/list machinery
    stays atomic.
    """
    group = "%s_%s_%s" % (group_prefix, str(controller), str(target))
    metadata = {
        "template": TEMPLATE_KIND_V2,
        "controller": controller,
        "when": dict(when or {}),
        "target": target,
        "then": dict(then or {}),
        "otherwise": dict(otherwise or {}),
    }
    lower = {
        "rule_kind": "conditional_lower",
        "constraint_group": group,
        "left_parameter": target,
        "operator": "gte",
        "right_parameter": controller,
        "multiplier": 0,
        "offset": 0,
        "template_metadata_json": json.dumps(metadata, ensure_ascii=False),
    }
    upper = {
        "rule_kind": "conditional_upper",
        "constraint_group": group,
        "left_parameter": target,
        "operator": "lte",
        "right_parameter": controller,
        "multiplier": 0,
        "offset": 0,
        "template_metadata_json": json.dumps(metadata, ensure_ascii=False),
    }
    return {"constraint_group": group, "template_metadata": metadata, "rules": [lower, upper]}


def affine_bound(rule, controller_value):
    """Evaluate one affine rule's right-hand side at a controller value."""
    multiplier = float(rule.get("multiplier") if rule.get("multiplier") is not None else 1)
    offset = float(rule.get("offset") or 0)
    return multiplier * float(controller_value) + offset


def parse_template_metadata(rule):
    raw = rule.get("template_metadata_json")
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return {}


def expected_range(template_metadata, controller_value):
    """Return the ``(lower, upper)`` bound a template implies for a controller value."""
    c = normalize_numeric(controller_value)
    a = normalize_numeric(template_metadata.get("active_value", 1))
    if c is not None and a is not None and abs(c - a) < 1e-9:
        return (float(template_metadata["active_min"]), float(template_metadata["active_max"]))
    return (float(template_metadata["inactive_value"]), float(template_metadata["inactive_value"]))


def build_rule_id(group, kind):
    return "%s_%s_%s" % (group, kind, uuid.uuid4().hex[:8].upper())
