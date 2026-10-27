# -*- coding: utf-8 -*-
"""Canonical persistence rules for presentation-only value mappings."""
from __future__ import print_function

import json
import math


def normalize_display_mapping(raw_mapping, allowed_values=None):
    """Return a canonical ``str business key -> str display label`` mapping.

    String, number and boolean labels are accepted for operator convenience and
    normalized before persistence. Nulls and structured labels are rejected.
    """
    if raw_mapping in (None, ""):
        return {}
    if isinstance(raw_mapping, str):
        try:
            raw_mapping = json.loads(raw_mapping)
        except (TypeError, ValueError):
            raise ValueError('前端显示映射必须是JSON对象，例如：{"0":"无","1":"有"}')
    if not isinstance(raw_mapping, dict):
        raise ValueError("前端显示映射必须是JSON对象。")

    normalized = {}
    for raw_key, raw_label in raw_mapping.items():
        key = str(raw_key).strip()
        if not key:
            raise ValueError("前端显示映射的业务值键不能为空。")
        if raw_label is None or isinstance(raw_label, (dict, list, tuple, set)):
            raise ValueError("前端显示映射的显示值只能是字符串或简单标量。")
        if isinstance(raw_label, float) and not math.isfinite(raw_label):
            raise ValueError("前端显示映射的显示值必须是有限的简单标量。")
        label = str(raw_label).strip()
        if not label:
            raise ValueError("前端显示映射的显示文本不能为空。")
        if key in normalized:
            raise ValueError("前端显示映射包含规范化后重复的业务值键：%s" % key)
        normalized[key] = label

    labels = list(normalized.values())
    if len(labels) != len(set(labels)):
        raise ValueError("同一指标的前端显示文本不能重复。")
    allowed = {str(value).strip() for value in (allowed_values or [])}
    invalid = [key for key in normalized if allowed and key not in allowed]
    if invalid:
        raise ValueError("前端显示映射包含不在业务允许值中的键：%s" % "、".join(invalid))
    assert all(isinstance(key, str) for key in normalized)
    assert all(isinstance(value, str) for value in normalized.values())
    return normalized


def dump_display_mapping(raw_mapping, allowed_values=None):
    mapping = normalize_display_mapping(raw_mapping, allowed_values)
    return json.dumps(mapping, ensure_ascii=False) if mapping else None
