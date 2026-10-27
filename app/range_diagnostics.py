# -*- coding: utf-8 -*-
"""Source-aware advisory range diagnostics.

DataMaster, model-contract and training ranges are metadata.  This module only
describes where a value sits; it never validates, rejects, clamps or rewrites it.
"""
from __future__ import print_function

import math

from .value_semantics import is_special_value, normalize_numeric, special_value_label


def _range(actual, lower, upper, source):
    if lower is None and upper is None:
        return None
    number = normalize_numeric(actual)
    try:
        lo = None if lower is None else float(lower)
        hi = None if upper is None else float(upper)
    except (TypeError, ValueError):
        return None
    inside = None
    if number is not None and math.isfinite(number):
        inside = (lo is None or number >= lo) and (hi is None or number <= hi)
    return {"min": lo, "max": hi, "inside": inside, "source": source}


def build_range_diagnostics(business_parameters, definitions, model_feature_specs=None, model_parameters=None):
    """Return one diagnostic per parameter with any declared range metadata."""
    business_parameters = dict(business_parameters or {})
    model_parameters = dict(model_parameters or business_parameters)
    definitions = definitions or {}
    specs_by_key = {}
    for spec in model_feature_specs or []:
        key = spec.get("key") or spec.get("parameter_id")
        kind = spec.get("model_kind") or spec.get("source_model")
        if key and kind in ("price", "effectiveness"):
            specs_by_key.setdefault(key, {})[kind] = spec

    keys = set(business_parameters) | set(specs_by_key)
    result = []
    for key in sorted(keys):
        definition = definitions.get(key) or {}
        actual = business_parameters.get(key)
        special = is_special_value(definition, actual)
        item = {
            "parameter_id": key,
            "label": definition.get("label") or key,
            "unit": definition.get("unit") or "",
            "actual": actual,
            "special_state": special,
            "special_state_label": special_value_label(definition, actual) if special else None,
            "business_reference": None if special else _range(actual, definition.get("min_value"), definition.get("max_value"), "data_master"),
            "model_contracts": {},
            "training_ranges": {},
        }
        model_actual = model_parameters.get(key, actual)
        for kind, spec in (specs_by_key.get(key) or {}).items():
            contract = None if special else _range(model_actual, spec.get("min"), spec.get("max"), "%s_schema" % kind)
            training = None if special else _range(model_actual, spec.get("training_min"), spec.get("training_max"), "%s_training" % kind)
            if contract is not None:
                item["model_contracts"][kind] = contract
            if training is not None:
                item["training_ranges"][kind] = training
        ranges = [item["business_reference"]] + list(item["model_contracts"].values()) + list(item["training_ranges"].values())
        ranges = [value for value in ranges if value is not None]
        if special:
            item["outside_any_reference"] = False
            result.append(item)
        elif ranges:
            item["outside_any_reference"] = any(value.get("inside") is False for value in ranges)
            result.append(item)
    return result
