# -*- coding: utf-8 -*-
"""Canonical business-semantics snapshot and portable offline package."""
from __future__ import print_function

import hashlib
import io
import json
import zipfile
from datetime import datetime


SEMANTIC_SCHEMA_VERSION = "2"
SEMANTIC_SECTIONS = (
    "products", "parameters", "parameter_groups", "tags", "tag_rules",
    "couplings", "constraints", "agreements", "model_inputs",
)
JSON_FIELDS = {
    "parameters": ("allowed_values_json", "special_value_keys_json",
                   "display_value_mapping_json", "model_value_mapping_json"),
    "constraints": ("template_metadata_json",),
}
PARAMETER_FIELDS = (
    "parameter_id", "label", "unit", "value_type", "search_type", "parameter_group",
    "required", "auto_adjustable", "min_value", "max_value", "observed_min", "observed_max",
    "preference", "description", "adjustment_hint", "decimal_places", "display_order",
    "enabled", "allowed_values_json", "special_value_keys_json",
    "display_value_mapping_json", "model_value_mapping_json",
)


def _decode_json(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def canonicalize_snapshot(snapshot):
    source = snapshot or {}
    result = {"semantic_schema_version": SEMANTIC_SCHEMA_VERSION}
    for section in SEMANTIC_SECTIONS:
        rows = []
        for raw in source.get(section) or []:
            item = dict(raw)
            if section == "parameters":
                item = dict((key, item.get(key)) for key in PARAMETER_FIELDS)
                for field in ("unit", "description", "adjustment_hint"):
                    item[field] = item.get(field) or ""
                item["preference"] = item.get("preference") or "neutral"
                item["parameter_group"] = item.get("parameter_group") or "其他"
            elif section == "products":
                item["product_description"] = item.get("product_description") or ""
            for field in JSON_FIELDS.get(section, ()):
                default = [] if field in ("allowed_values_json", "special_value_keys_json") else {}
                item[field[:-5] if field.endswith("_json") else field] = _decode_json(item.pop(field, None), default)
            rows.append(item)
        primary = {
            "products": "product_code", "parameters": "parameter_id",
            "parameter_groups": "group_name", "tags": "tag_id", "tag_rules": "rule_id",
            "couplings": "coupling_id", "constraints": "rule_id",
            "agreements": "agreement_id", "model_inputs": "binding_id",
        }.get(section)
        result[section] = sorted(rows, key=lambda row: str(row.get(primary) or "")) if primary else rows
    return result


def semantic_signature(snapshot):
    canonical = canonicalize_snapshot(snapshot)
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SemanticSnapshotService(object):
    def __init__(self, store, data_master, runtime_contract_provider=None):
        self.store = store
        self.data_master = data_master
        self.runtime_contract_provider = runtime_contract_provider

    def build(self, snapshot=None):
        canonical = canonicalize_snapshot(snapshot if snapshot is not None else self.store.admin_snapshot())
        canonical["semantic_signature"] = semantic_signature(canonical)
        return canonical

    def package(self):
        source = self.store.admin_snapshot()
        semantic = self.build(source)
        product_code = ((semantic.get("products") or [{}])[0].get("product_code") or "product")
        contract = self.runtime_contract_provider() if self.runtime_contract_provider else {}
        manifest = {
            "semantic_schema_version": SEMANTIC_SCHEMA_VERSION,
            "product_code": product_code,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "semantic_signature": semantic["semantic_signature"],
            "parameter_count": len(semantic.get("parameters") or []),
            "model_bound_semantics": "derived_from_model_input_bindings",
        }
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("semantic_snapshot.json", json.dumps(semantic, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr("datamaster.xlsx", self.data_master.export_current())
            archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"))
            archive.writestr("runtime_contract.json", json.dumps(contract, ensure_ascii=False, indent=2).encode("utf-8"))
        return output.getvalue(), "%s_semantic_package.zip" % product_code
