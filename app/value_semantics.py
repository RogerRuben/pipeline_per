# -*- coding: utf-8 -*-
"""Unified business-value semantics shared by filtering, generation and display.

One source of truth for the questions that keep recurring across the codebase:

* is ``1`` / ``1.0`` / ``"1"`` / ``"1.0"`` the same value?  (yes, numerically)
* is ``1.0`` the same boolean as ``"有"``?                 (yes, both truthy)
* how do we render a stored ``1`` back to the operator?    (the independent display label)

The model encoding stays untouched: ``Store.runtime_parameters()`` continues to
own the business-value -> model-value mapping.  This module only normalises
comparisons and display so every consumer uses identical semantics.
"""
from __future__ import print_function

import json
import math

_TRUE_TOKENS = {"1", "1.0", "true", "yes", "y", "on", "是", "有", "具备", "支持", "启用"}
_FALSE_TOKENS = {"0", "0.0", "false", "no", "n", "off", "否", "无", "不具备", "不支持", "停用", ""}


def normalize_numeric(value):
    """Return ``float(value)`` or ``None``. ``"IP65"``/``65`` both yield ``65.0``."""
    if value is None:
        return None
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    text = str(value).strip()
    if text.upper().startswith("IP"):
        text = text[2:].strip()
    if text == "":
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def normalize_boolean(value):
    """Return ``True``/``False`` for any legal boolean form, else ``None``.

    Recognises ``True/1/1.0/"1"/"1.0"/"true"/"yes"/"是"/"有"`` as truthy and
    ``False/0/0.0/"0"/"0.0"/"false"/"no"/"否"/"无"`` as falsy.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number == 1.0:
            return True
        if number == 0.0:
            return False
        return None
    text = str(value).strip().lower()
    if text in _TRUE_TOKENS:
        return True
    if text in _FALSE_TOKENS:
        return False
    return None


def definition_value_type(definition):
    if not definition:
        return None
    return str(definition.get("value_type") or definition.get("dtype") or "").strip().lower()


def definition_mapping(definition):
    """Return the business-value -> model-value mapping of a definition, if any."""
    if not definition:
        return {}
    raw = definition.get("model_value_mapping_json")
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items()}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {str(k): v for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def display_definition_mapping(definition):
    """Return canonical-business-value -> presentation-label mapping.

    This mapping is deliberately independent from ``model_value_mapping_json``.
    It must never participate in matching, persistence, hashing, generation or
    model requests.
    """
    if not definition:
        return {}
    raw = definition.get("display_value_mapping_json")
    if raw in (None, ""):
        return {}
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return {str(k): str(v) for k, v in parsed.items()} if isinstance(parsed, dict) else {}


def special_value_keys(definition):
    """Return explicitly declared special business-state keys.

    Display mappings are presentation-only and are intentionally not inferred as
    special states.  Keys remain canonical strings in metadata while comparison
    uses :func:`values_equal` against the original business value.
    """
    if not definition:
        return []
    raw = definition.get("special_value_keys_json")
    if raw in (None, ""):
        return []
    if isinstance(raw, (list, tuple)):
        values = list(raw)
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        values = parsed if isinstance(parsed, list) else []
    result = []
    for value in values:
        key = str(value).strip()
        if key and key not in result:
            result.append(key)
    return result


def is_special_value(definition, value):
    if value is None or value == "":
        return False
    return any(values_equal(value, key, definition) for key in special_value_keys(definition))


def special_value_label(definition, value):
    if not is_special_value(definition, value):
        return None
    return business_display_value(value, definition)


def normal_numeric_values(definition, values):
    """Exclude structural special states from an otherwise numeric domain."""
    return [value for value in (values or []) if not is_special_value(definition, value)]


def mapping_target(value, definition=None):
    """Return the model value a business label/encoding maps to, else ``None``.

    Unlike :func:`canonical_filter_value` this never falls back to boolean truth,
    so a declared third state (``无该属性 -> -1``) survives to the caller.
    """
    mapping = definition_mapping(definition)
    if not mapping:
        return None
    text = str(value).strip()
    for business, model in mapping.items():
        if str(business).strip() == text:
            return model
    number = normalize_numeric(value)
    if number is not None:
        for business, model in mapping.items():
            business_num = normalize_numeric(business)
            if business_num is not None and math.isclose(business_num, number, rel_tol=1e-9, abs_tol=1e-9):
                return model
    return None


def values_equal(left, right, definition=None):
    """Compare two business values with boolean/numeric tolerance.

    Boolean-typed fields compare as booleans; everything else compares
    numerically when both sides are numeric and falls back to trimmed strings.
    """
    value_type = definition_value_type(definition)
    if value_type in ("boolean", "bool"):
        left_bool = normalize_boolean(left)
        right_bool = normalize_boolean(right)
        if left_bool is not None and right_bool is not None:
            return left_bool == right_bool
        return str(left).strip() == str(right).strip()
    left_num = normalize_numeric(left)
    right_num = normalize_numeric(right)
    if left_num is not None and right_num is not None:
        decimal_places = int((definition or {}).get("decimal_places", 3) or 3)
        tolerance = 10.0 ** (-max(2, decimal_places))
        return abs(left_num - right_num) <= tolerance
    return str(left).strip() == str(right).strip()


def canonical_filter_value(value, definition=None):
    """Convert a business filter value to the canonical comparison/model form."""
    value_type = definition_value_type(definition)
    if value_type in ("boolean", "bool"):
        return normalize_boolean(value)
    if value_type == "ip_grade":
        number = normalize_numeric(value)
        return number
    mapping = definition_mapping(definition)
    if mapping:
        text = str(value).strip()
        for business, model in mapping.items():
            if business.strip() == text:
                return model
        # numeric business labels may be written "1.0"
        number = normalize_numeric(value)
        for business, model in mapping.items():
            business_num = normalize_numeric(business)
            if business_num is not None and number is not None and math.isclose(business_num, number, rel_tol=1e-9, abs_tol=1e-9):
                return model
    return value


def business_display_value(value, definition=None):
    """Render a canonical business value without changing that value."""
    if value is None:
        return ""
    display_mapping = display_definition_mapping(definition)
    if display_mapping:
        text = str(value).strip()
        if text in display_mapping:
            return display_mapping[text]
        number = normalize_numeric(value)
        if number is not None:
            for business, label in display_mapping.items():
                business_number = normalize_numeric(business)
                if business_number is not None and math.isclose(business_number, number, rel_tol=1e-9, abs_tol=1e-9):
                    return label
    value_type = definition_value_type(definition)
    if value_type in ("boolean", "bool"):
        boolean = normalize_boolean(value)
        if boolean is True:
            return "有"
        if boolean is False:
            return "无"
    if value_type == "ip_grade":
        number = normalize_numeric(value)
        if number is not None:
            return "IP%d" % int(number) if float(number).is_integer() else "IP%s" % value
        return "IP%s" % value
    return str(value)


def nice_engineering_step(raw_step, decimal_places=None):
    """Snap a raw search step to an engineering-friendly increment.

    The step is snapped to the nearest ``1 / 2 / 2.5 / 5 / 10`` multiplied by a
    power of ten, so continuous parameters move on a readable grid (``4.5 -> 4.45``)
    instead of a standard-deviation-derived float (``4.5 -> 4.44112137``).
    """
    if raw_step is None or raw_step <= 0:
        return None
    magnitude = math.floor(math.log10(raw_step))
    base = 10.0 ** magnitude
    fraction = raw_step / base
    nearest = min((1.0, 2.0, 2.5, 5.0, 10.0), key=lambda candidate: abs(candidate - fraction))
    return nearest * base


def canonicalize_parameter_value(definition, value):
    """Round a parameter value to its business precision (boolean/int/decimal)."""
    if value is None:
        return value
    definition = definition or {}
    value_type = definition_value_type(definition)
    if value_type in ("boolean", "bool"):
        boolean = normalize_boolean(value)
        if boolean is not None:
            return 1.0 if boolean else 0.0
    if value_type == "ip_grade":
        number = normalize_numeric(value)
        return int(number) if number is not None else value
    number = normalize_numeric(value)
    if number is None:
        return value
    search_type = str(definition.get("search_type") or "auto").lower()
    if search_type in ("integer", "ordered_discrete"):
        return int(round(number))
    if value_type in ("integer",):
        return int(round(number))
    decimal_places = int(definition.get("decimal_places", 3) or 3)
    return round(number, decimal_places)
