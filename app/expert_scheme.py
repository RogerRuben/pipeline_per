# -*- coding: utf-8 -*-
"""Business semantics for the saved expert-prior library.

SQL persistence stays in :mod:`app.store`; this module owns definition-aware
delta construction, schema compatibility and deterministic training exports.
"""
from __future__ import print_function

import hashlib
import json

from .value_semantics import (
    canonicalize_parameter_value, definition_value_type, is_special_value,
    normalize_boolean, normalize_numeric, values_equal,
)


class ExpertSchemeService(object):
    def __init__(self, definitions, product_code, runtime=None, encode_parameters=None):
        self.definitions = dict(definitions or {})
        self.product_code = str(product_code or "")
        self.runtime = runtime
        self.encode_parameters = encode_parameters

    def build_delta(self, base_parameters, final_parameters):
        base = dict(base_parameters or {})
        final = dict(final_parameters or {})
        delta = {}
        for parameter_id in sorted(set(base) | set(final)):
            before = base.get(parameter_id)
            after = final.get(parameter_id)
            definition = self.definitions.get(parameter_id)
            if not values_equal(before, after, definition):
                delta[parameter_id] = {"before": before, "after": after}
        return delta

    def schema_signature(self):
        rows = []
        for parameter_id, definition in sorted(self.definitions.items()):
            if int(definition.get("enabled", 1) or 0) == 0:
                continue
            rows.append({
                "parameter_id": parameter_id,
                "value_type": definition.get("value_type"),
                "required": int(definition.get("required", 0) or 0),
                "model_bound": int(definition.get("model_bound", 1) or 0),
                "model_value_mapping_json": definition.get("model_value_mapping_json"),
                "allowed_values_json": definition.get("allowed_values_json"),
                "special_value_keys_json": definition.get("special_value_keys_json"),
                "search_type": definition.get("search_type"),
            })
        encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def canonical_parameter_signature(self, params):
        canonical = {}
        for key, value in sorted(dict(params or {}).items()):
            definition = self.definitions.get(key)
            if not definition or int(definition.get("enabled", 1) or 0) == 0:
                continue
            canonical[key] = canonicalize_parameter_value(definition, value)
        encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    @staticmethod
    def _allowed_values(definition):
        raw = (definition or {}).get("allowed_values_json")
        if raw in (None, ""):
            return []
        if isinstance(raw, list):
            return list(raw)
        try:
            parsed = json.loads(raw)
            return list(parsed) if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []

    def _business_value_issue(self, parameter_id, value, definition):
        if value in (None, ""):
            return None
        if is_special_value(definition, value):
            return None
        allowed = self._allowed_values(definition)
        if allowed and not any(values_equal(value, candidate, definition) for candidate in allowed):
            return {"parameter_id": parameter_id, "value": value, "reason": "not_in_current_allowed_values"}
        value_type = definition_value_type(definition)
        if value_type in ("boolean", "bool") and normalize_boolean(value) is None:
            return {"parameter_id": parameter_id, "value": value, "reason": "invalid_boolean_business_value"}
        if value_type in ("number", "float", "integer", "ip_grade") and normalize_numeric(value) is None:
            return {"parameter_id": parameter_id, "value": value, "reason": "not_numeric"}
        return None

    def compatibility(self, scheme):
        params = dict((scheme or {}).get("params") or {})
        product_match = bool(self.product_code) and str((scheme or {}).get("product_code") or "") == self.product_code
        enabled = dict((key, value) for key, value in self.definitions.items()
                       if int(value.get("enabled", 1) or 0) != 0)
        unknown = sorted(key for key in params if key not in enabled)
        required = [key for key, value in enabled.items()
                    if int(value.get("required", 0) or 0) != 0 and int(value.get("model_bound", 1) or 0) != 0]
        missing = sorted(key for key in required if params.get(key) in (None, ""))
        value_issues = []
        for key, value in params.items():
            if key in enabled:
                issue = self._business_value_issue(key, value, enabled[key])
                if issue:
                    value_issues.append(issue)
        conversion_error = None
        contract_probe_warning = None
        if product_match and not missing and self.runtime is not None:
            try:
                # This is a non-evaluating contract probe.  Actual model-service
                # rejection remains authoritative when the candidate is used.
                if hasattr(self.runtime, "all_feature_specs"):
                    specs = list(self.runtime.all_feature_specs() or [])
                    required_model = [str(x.get("parameter_id") or x.get("name") or x.get("field_name") or "") for x in specs
                                      if x.get("required") and (x.get("parameter_id") or x.get("name") or x.get("field_name"))]
                    missing.extend(key for key in required_model if params.get(key) in (None, ""))
            except Exception as exc:
                contract_probe_warning = str(exc)
        if product_match and not missing and not value_issues and self.encode_parameters is not None:
            try:
                encoded = self.encode_parameters(params)
                if not isinstance(encoded, dict):
                    raise ValueError("runtime encoding did not return a mapping")
            except Exception as exc:
                conversion_error = str(exc)
        missing = sorted(set(missing))
        # A temporarily unavailable model service is not evidence that the
        # persisted business snapshot is schema-incompatible.  Missing current
        # required fields are authoritative here; an actual service rejection
        # remains authoritative when evaluation is attempted.
        field_shape_compatible = not missing
        business_value_compatible = not value_issues
        model_input_compatible = conversion_error is None
        schema_compatible = field_shape_compatible and business_value_compatible and model_input_compatible
        current_signature = self.schema_signature()
        stored_signature = str((scheme or {}).get("schema_signature") or "")
        stored_eligible = bool(int((scheme or {}).get("recommendation_eligible", 1) or 0))
        record_enabled = bool(int((scheme or {}).get("enabled", 1) or 0))
        effective = product_match and schema_compatible and stored_eligible and record_enabled
        return {
            "product_match": product_match,
            "schema_compatible": schema_compatible,
            "field_shape_compatible": field_shape_compatible,
            "business_value_compatible": business_value_compatible,
            "model_input_compatible": model_input_compatible,
            "schema_changed": bool(stored_signature and stored_signature != current_signature),
            "current_schema_signature": current_signature,
            "missing_fields": missing,
            "unknown_fields": unknown,
            "business_value_issues": value_issues,
            "conversion_error": conversion_error,
            "contract_probe_warning": contract_probe_warning,
            "recommendation_eligible": stored_eligible,
            "recommendation_eligible_effective": effective,
        }

    @staticmethod
    def training_export_record(scheme):
        return {
            "scheme_id": scheme.get("id"),
            "scheme_name": scheme.get("scheme_name"),
            "product_code": scheme.get("product_code"),
            "base_agreement_id": scheme.get("base_agreement_id"),
            "root_base_agreement_id": scheme.get("root_base_agreement_id"),
            "parent_saved_scheme_id": scheme.get("parent_saved_scheme_id"),
            "expert_revision_no": scheme.get("expert_revision_no"),
            "source": scheme.get("source_type") or "expert_saved",
            "base_parameters": dict(scheme.get("base_params") or {}),
            "parameters": dict(scheme.get("params") or {}),
            "delta": dict(scheme.get("delta") or {}),
            "changed_parameter_ids": list(scheme.get("changed_parameter_ids") or []),
            "evaluation": dict(scheme.get("evaluation") or scheme.get("saved_evaluation") or {}),
            "target_protocol": scheme.get("target_protocol"),
            "schema_signature": scheme.get("schema_signature"),
            "risk_confirmed": bool(scheme.get("risk_confirmed")),
            "recommendation_eligible": bool(scheme.get("recommendation_eligible")),
            "training_candidate": bool(scheme.get("training_candidate")),
            "created_at": scheme.get("created_at"),
        }
