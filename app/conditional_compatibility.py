# -*- coding: utf-8 -*-
"""Save-time compatibility checks for conditional relationship metadata."""
from __future__ import print_function

from .value_semantics import normalize_numeric


def validate_conditional_relationship(metadata, definitions=None, model_specs=None):
    """Return compatibility with business and model errors separated.

    ``business_errors`` are DataMaster-level configuration errors and must always
    block saving.  ``model_errors`` are model-contract conflicts; the caller may
    allow saving when model services are offline.
    """
    definitions = definitions or {}
    model_specs = model_specs or []
    business_errors = []
    model_errors = []
    warnings = []
    model_compatibility = {}
    target = metadata.get("target")
    definition = definitions.get(target) or {}
    target_min = definition.get("min_value")
    target_max = definition.get("max_value")

    for branch_name in ("then", "otherwise"):
        branch = metadata.get(branch_name) or {}
        mode = branch.get("mode") or "not_applicable"
        if mode in ("not_applicable", "fixed"):
            model_value = branch.get("model_value")
            if model_value is not None and target_min is not None and target_max is not None:
                number = normalize_numeric(model_value)
                if number is not None and (number < float(target_min) or number > float(target_max)):
                    warnings.append(
                        "从属指标「%s」的%s模型值 %s 位于DataMaster参考范围 %s~%s 外；仍允许保存。"
                        % (target, branch_name, model_value, target_min, target_max)
                    )
        elif mode == "range":
            lo = normalize_numeric(branch.get("min"))
            hi = normalize_numeric(branch.get("max"))
            if lo is not None and hi is not None and lo > hi:
                business_errors.append("从属指标「%s」的%s范围下限高于上限。" % (target, branch_name))
            if lo is not None and target_min is not None and lo < float(target_min):
                warnings.append("从属指标「%s」的%s范围下限低于指标允许下限。" % (target, branch_name))
            if hi is not None and target_max is not None and hi > float(target_max):
                warnings.append("从属指标「%s」的%s范围上限高于指标允许上限。" % (target, branch_name))

    # Model-contract checks: use each model spec's own min/max when available.
    for spec in model_specs or []:
        if spec.get("key") != target:
            continue
        spec_min = spec.get("min")
        spec_max = spec.get("max")
        spec_errors = []
        for branch_name in ("then", "otherwise"):
            branch = metadata.get(branch_name) or {}
            mode = branch.get("mode") or "not_applicable"
            if mode in ("not_applicable", "fixed"):
                model_value = normalize_numeric(branch.get("model_value"))
                if model_value is not None:
                    if spec_min is not None and model_value < float(spec_min):
                        spec_errors.append("%s模型值 %s 低于模型允许下限 %s" % (branch_name, branch.get("model_value"), spec_min))
                    if spec_max is not None and model_value > float(spec_max):
                        spec_errors.append("%s模型值 %s 高于模型允许上限 %s" % (branch_name, branch.get("model_value"), spec_max))
            elif mode == "range":
                lo = normalize_numeric(branch.get("min"))
                hi = normalize_numeric(branch.get("max"))
                if lo is not None and spec_min is not None and lo < float(spec_min):
                    spec_errors.append("%s范围下限 %s 低于模型允许下限 %s" % (branch_name, branch.get("min"), spec_min))
                if hi is not None and spec_max is not None and hi > float(spec_max):
                    spec_errors.append("%s范围上限 %s 高于模型允许上限 %s" % (branch_name, branch.get("max"), spec_max))
        model_compatibility[spec.get("model_kind") or spec.get("model_role") or "model"] = {
            "compatible": not spec_errors,
            "errors": spec_errors,
        }
        warnings.extend("模型Schema参考范围提示：%s" % error for error in spec_errors)

    errors = list(business_errors)
    return {
        "compatible": not errors,
        "errors": errors,
        "business_errors": business_errors,
        "model_errors": model_errors,
        "warnings": warnings,
        "model_compatibility": model_compatibility,
    }
