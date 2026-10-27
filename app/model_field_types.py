# -*- coding: utf-8 -*-
"""Shared model/business field-type compatibility rules."""
from __future__ import print_function

import re


def _clean(value):
    return str(value or "").strip().lower()


def canonical_field_id(header, index, used=None):
    """Stable English field id for a data column header.

    A valid English identifier is kept as-is; anything else (Chinese headers,
    unit suffixes such as ``额定载荷(N)``, mixed ``压力_bar``) falls back to
    ``attr_%03d`` in column order.  The price export, historical-product
    onboarding and effectiveness workbook all use this convention so the three
    schemas converge on the same field ids without manual mapping.
    """
    label = str(header or "").strip()
    candidate = label if re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", label) else "attr_%03d" % int(index)
    if used is None:
        return candidate
    base = candidate
    suffix = 2
    while candidate in used:
        candidate = "%s_%d" % (base, suffix)
        suffix += 1
    used.add(candidate)
    return candidate


def canonical_model_type(value):
    aliases = {
        "numeric": "number",
        "float": "number",
        "continuous": "number",
        "int": "integer",
        "bool": "boolean",
        "categorical": "enum",
        "category": "enum",
        "text": "enum",
    }
    cleaned = _clean(value)
    return aliases.get(cleaned, cleaned)


def model_types_compatible(actual, expected):
    """Return True when two declarations share the same wire representation.

    Business metadata may preserve a more useful editor type (IP grade,
    boolean, or integer) while an effectiveness runtime exposes the value as a
    numeric feature.  Those pairs are compatible; enum and free numeric fields
    remain deliberately distinct.
    """
    actual = canonical_model_type(actual)
    expected = canonical_model_type(expected)
    if actual == expected:
        return True
    compatible_groups = (
        {"number", "integer"},
        {"integer", "ip_grade"},
        {"integer", "boolean"},
        {"number", "boolean"},
        {"number", "ip_grade"},
    )
    return any(set((actual, expected)).issubset(group) for group in compatible_groups)
