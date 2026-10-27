# -*- coding: utf-8 -*-
from __future__ import print_function

import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

from .model_field_types import model_types_compatible
from .display_mapping import dump_display_mapping, normalize_display_mapping
from .value_semantics import is_special_value, normalize_numeric

from .recommender import filter_match


def now_iso():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def safe_id(prefix):
    return "%s-%s" % (prefix, uuid.uuid4().hex[:10].upper())


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _json_list(value):
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return [x.strip() for x in str(value).replace("，", ",").replace("、", ",").split(",") if x.strip()]


def _json_mapping(value):
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _json_object(value):
    """Parse a JSON object defensively; a malformed blob never crashes the UI."""
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _infer_model_value_mapping(parameter, model_spec):
    """Infer only unambiguous business-label to canonical-model encodings."""
    value_type = str(parameter.get("value_type") or "").lower()
    model_type = str(model_spec.get("dtype") or model_spec.get("type") or "").lower()
    if value_type == "boolean" or model_type in ("bool", "boolean"):
        return {
            "是": 1, "有": 1, "启用": 1, "具备": 1, "支持": 1,
            "否": 0, "无": 0, "停用": 0, "不具备": 0, "不支持": 0,
            "true": 1, "false": 0, "yes": 1, "no": 0,
        }
    business_values = _json_list(parameter.get("allowed_values_json"))
    model_values = list(model_spec.get("allowed_values") or [])
    if not business_values or not model_values or len(business_values) != len(model_values):
        return {}
    if set(str(value).strip().lower() for value in business_values) == set(str(value).strip().lower() for value in model_values):
        return {}
    numbered = []
    for value in business_values:
        match = re.search(r"(?:类型|类别|方案|等级|type)\s*([0-9]+)$", str(value).strip(), re.I)
        if not match:
            return {}
        numbered.append((int(match.group(1)), value))
    try:
        ordered_model = sorted(model_values, key=lambda value: float(value))
    except (TypeError, ValueError):
        return {}
    numbered.sort(key=lambda item: item[0])
    if [item[0] for item in numbered] != list(range(1, len(numbered) + 1)):
        return {}
    return dict((str(source), target) for (_number, source), target in zip(numbered, ordered_model))


class Store(object):
    REQUIRED_TABLES = {
        "metadata", "products", "parameter_definitions", "tags", "agreements",
        "saved_schemes", "model_registry", "model_input_bindings", "indicator_couplings", "constraint_rules",
        "tag_rules", "audit_log", "product_releases", "parameter_groups",
    }
    MIGRATABLE_TABLES = {"product_releases"}

    def __init__(self, db_path, dataset_path, model_runtime, backup_dir=None, read_only=False):
        self.db_path = Path(db_path)
        self.dataset_path = Path(dataset_path)
        self.runtime = model_runtime
        self.backup_dir = Path(backup_dir or self.db_path.parent.parent / "backups")
        self.read_only = bool(read_only)
        self.lock = threading.RLock()
        if self.read_only:
            if not self.db_path.is_file():
                raise RuntimeError("只读演示数据库不存在：%s" % self.db_path)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self.backup_dir.mkdir(parents=True, exist_ok=True)
            self._initialize()

    def connect(self, path=None):
        target = Path(path) if path is not None else self.db_path
        use_read_only = self.read_only and target.resolve() == self.db_path.resolve()
        if use_read_only:
            uri = "file:%s?mode=ro" % target.resolve().as_posix()
            conn = sqlite3.connect(uri, timeout=30, uri=True)
        else:
            conn = sqlite3.connect(str(target), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _columns(conn, table):
        return set(row[1] for row in conn.execute("PRAGMA table_info(%s)" % table))

    def _add_column(self, conn, table, definition):
        name = definition.split()[0]
        if name not in self._columns(conn, table):
            conn.execute("ALTER TABLE %s ADD COLUMN %s" % (table, definition))

    def _initialize(self):
        with self.lock:
            conn = self.connect()
            try:
                conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS products(
                    product_code TEXT PRIMARY KEY, product_name TEXT NOT NULL, product_description TEXT, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS parameter_definitions(
                    parameter_id TEXT PRIMARY KEY, label TEXT NOT NULL, unit TEXT, value_type TEXT NOT NULL,
                    min_value REAL, max_value REAL, observed_min REAL, observed_max REAL, preference TEXT, description TEXT, adjustment_hint TEXT,
                    allowed_values_json TEXT, model_value_mapping_json TEXT, display_value_mapping_json TEXT, special_value_keys_json TEXT,
                    search_type TEXT NOT NULL DEFAULT 'auto', required INTEGER NOT NULL DEFAULT 1,
                    auto_adjustable INTEGER NOT NULL DEFAULT 1, decimal_places INTEGER NOT NULL DEFAULT 3,
                    parameter_group TEXT NOT NULL DEFAULT '其他',
                    display_order INTEGER NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, model_bound INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS parameter_groups(
                    group_name TEXT PRIMARY KEY, display_order INTEGER NOT NULL DEFAULT 1,
                    description TEXT, enabled INTEGER NOT NULL DEFAULT 1, default_collapsed INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS tags(
                    tag_id TEXT PRIMARY KEY, tag_name TEXT NOT NULL, tag_group TEXT, weight REAL NOT NULL,
                    derivation_mode TEXT NOT NULL DEFAULT 'rule', description TEXT, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS tag_rules(
                    rule_id TEXT PRIMARY KEY, tag_id TEXT NOT NULL, parameter_id TEXT NOT NULL,
                    operator TEXT NOT NULL, value1 TEXT, value2 TEXT, rule_group TEXT NOT NULL DEFAULT 'default',
                    enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS agreements(
                    agreement_id TEXT PRIMARY KEY, product_code TEXT NOT NULL, agreement_name TEXT NOT NULL,
                    positioning TEXT, agreement_source TEXT NOT NULL, source_year INTEGER, supplier_type TEXT,
                    historical_price_wan REAL, capability_score REAL, feasibility_probability REAL,
                    params_json TEXT NOT NULL, tags_json TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS saved_schemes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scheme_name TEXT NOT NULL, base_agreement_id TEXT,
                    product_code TEXT, source_type TEXT, params_json TEXT NOT NULL, evaluation_json TEXT NOT NULL,
                    risk_confirmed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_registry(
                    model_kind TEXT PRIMARY KEY, model_version TEXT NOT NULL, product_code TEXT,
                    artifact_path TEXT NOT NULL, artifact_sha256 TEXT, contract_status TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_input_bindings(
                    binding_id TEXT PRIMARY KEY, model_kind TEXT NOT NULL, parameter_id TEXT NOT NULL,
                    label TEXT, source_type TEXT, data_type TEXT, unit TEXT,
                    required INTEGER NOT NULL DEFAULT 0, missing_policy TEXT NOT NULL DEFAULT 'reject',
                    configured_value TEXT, training_mean REAL, model_version TEXT,
                    enabled INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS indicator_couplings(
                    coupling_id TEXT PRIMARY KEY, coupling_name TEXT NOT NULL, coupling_type TEXT NOT NULL,
                    parameter_a TEXT NOT NULL, parameter_b TEXT NOT NULL, domain_operator TEXT,
                    multiplier REAL, offset REAL, strength REAL, severity TEXT, description TEXT, rationale TEXT,
                    display_order INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS constraint_rules(
                    rule_id TEXT PRIMARY KEY, rule_name TEXT NOT NULL, left_parameter TEXT NOT NULL,
                    operator TEXT NOT NULL, right_parameter TEXT, multiplier REAL NOT NULL DEFAULT 1,
                    offset REAL NOT NULL DEFAULT 0, severity TEXT, message TEXT, rationale TEXT,
                    display_order INTEGER NOT NULL DEFAULT 1, enabled INTEGER NOT NULL DEFAULT 1,
                    rule_kind TEXT NOT NULL DEFAULT 'affine', constraint_group TEXT, template_metadata_json TEXT
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, object_type TEXT,
                    object_id TEXT, detail_json TEXT, created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS product_releases(
                    release_id TEXT PRIMARY KEY, product_code TEXT NOT NULL, product_name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'draft', data_json TEXT NOT NULL,
                    validation_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    activated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS requirement_versions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, product_code TEXT NOT NULL,
                    version_no INTEGER NOT NULL, parent_version_id INTEGER,
                    demand_json TEXT NOT NULL, demand_fingerprint TEXT NOT NULL,
                    change_summary_json TEXT NOT NULL, target_protocol TEXT,
                    created_at TEXT NOT NULL, created_by TEXT,
                    UNIQUE(product_code, version_no)
                );
                CREATE INDEX IF NOT EXISTS idx_requirement_versions_product
                    ON requirement_versions(product_code, version_no DESC);
                CREATE TABLE IF NOT EXISTS final_decisions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, scheme_id TEXT,
                    scheme_snapshot_json TEXT NOT NULL, source TEXT,
                    demand_version_id INTEGER, product_code TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """)
                for table, definition in [
                    ("products", "enabled INTEGER NOT NULL DEFAULT 1"),
                    ("parameter_definitions", "enabled INTEGER NOT NULL DEFAULT 1"),
                    ("parameter_definitions", "model_bound INTEGER NOT NULL DEFAULT 1"),
                    ("parameter_definitions", "allowed_values_json TEXT"),
                    ("parameter_definitions", "model_value_mapping_json TEXT"),
                    ("parameter_definitions", "display_value_mapping_json TEXT"),
                    ("parameter_definitions", "special_value_keys_json TEXT"),
                    ("parameter_definitions", "search_type TEXT NOT NULL DEFAULT 'auto'"),
                    ("parameter_definitions", "required INTEGER NOT NULL DEFAULT 1"),
                    ("parameter_definitions", "auto_adjustable INTEGER NOT NULL DEFAULT 1"),
                    ("parameter_definitions", "decimal_places INTEGER NOT NULL DEFAULT 3"),
                    ("parameter_definitions", "parameter_group TEXT NOT NULL DEFAULT '其他'"),
                    ("parameter_definitions", "observed_min REAL"),
                    ("parameter_definitions", "observed_max REAL"),
                    ("tags", "enabled INTEGER NOT NULL DEFAULT 1"),
                    ("tags", "derivation_mode TEXT NOT NULL DEFAULT 'rule'"),
                    ("tags", "description TEXT"),
                    ("indicator_couplings", "strength REAL"),
                    ("agreements", "updated_at TEXT"),
                    ("model_registry", "artifact_sha256 TEXT"),
                    ("model_registry", "contract_status TEXT"),
                    ("tags", "archived_at TEXT"),
                    ("tag_rules", "archived_at TEXT"),
                    ("indicator_couplings", "archived_at TEXT"),
                    ("constraint_rules", "archived_at TEXT"),
                    ("constraint_rules", "rule_kind TEXT NOT NULL DEFAULT 'affine'"),
                    ("constraint_rules", "constraint_group TEXT"),
                    ("constraint_rules", "template_metadata_json TEXT"),
                    ("agreements", "archived_at TEXT"),
                    ("saved_schemes", "product_code TEXT"),
                    ("saved_schemes", "base_params_json TEXT"),
                    ("saved_schemes", "delta_json TEXT"),
                    ("saved_schemes", "changed_parameter_ids_json TEXT"),
                    ("saved_schemes", "target_protocol TEXT"),
                    ("saved_schemes", "schema_signature TEXT"),
                    ("saved_schemes", "recommendation_eligible INTEGER NOT NULL DEFAULT 1"),
                    ("saved_schemes", "training_candidate INTEGER NOT NULL DEFAULT 1"),
                    ("saved_schemes", "enabled INTEGER NOT NULL DEFAULT 1"),
                    ("saved_schemes", "expert_revision_no INTEGER"),
                    ("saved_schemes", "root_base_agreement_id TEXT"),
                    ("saved_schemes", "parent_saved_scheme_id INTEGER"),
                ]:
                    self._add_column(conn, table, definition)
                self._migrate_saved_scheme_lineage(conn)
                # Bootstrap managed parameter groups from existing definitions.
                group_rows = conn.execute(
                    "SELECT parameter_group, MIN(display_order) AS ord FROM parameter_definitions "
                    "WHERE parameter_group IS NOT NULL AND parameter_group<>'' GROUP BY parameter_group ORDER BY ord, parameter_group"
                ).fetchall()
                for row in group_rows:
                    conn.execute(
                        "INSERT OR IGNORE INTO parameter_groups(group_name, display_order, description, enabled, default_collapsed) "
                        "VALUES(?,?,?,1,0)", (row["parameter_group"], int(row["ord"] or 9999), "")
                    )

                conn.execute(
                    "INSERT OR IGNORE INTO parameter_groups(group_name, display_order, description, enabled, default_collapsed) "
                    "VALUES('其他',9999,'',1,0)"
                )
                # Normalize legacy IP-grade definitions. Older packages stored a
                # handful of observed grades as if they were the full legal set;
                # that prevented exploration of valid intermediate integer grades.
                conn.execute(
                    "UPDATE parameter_definitions SET search_type='integer', allowed_values_json=NULL "
                    "WHERE value_type='ip_grade' AND (search_type IS NULL OR search_type='' OR search_type='auto')"
                )
                self._sync_model_registry(conn)
                conn.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("database_version", "V19.6.8-hybrid-deep-extrapolation-search"))
                conn.execute("INSERT OR IGNORE INTO metadata VALUES(?,?)", ("master_data_version", "0"))
                conn.commit()
            finally:
                conn.close()

    def is_empty(self):
        conn = self.connect()
        try:
            return conn.execute("SELECT COUNT(*) FROM parameter_definitions").fetchone()[0] == 0
        finally:
            conn.close()

    def sync_model_schema(self):
        """Diagnose field-role and type/unit drift without mutating DataMaster.

        DataMaster is the business authority: the operator owns the field set,
        ``enabled`` / ``required`` / engineering ``min``/``max`` / preference and
        value mappings.  The model Schema is only a runtime contract.  This
        method reports mismatches so the operator can fix DataMaster explicitly;
        it never INSERTs or UPDATEs business rows, and it never flips an
        operator's ``enabled`` flag back on.

        Field roles (shared / effectiveness-only / price-only) are computed from
        the two service schemas elsewhere via ``feature_roles``; they never
        depend on a mirrored table.
        """
        warnings = []
        with self.lock:
            conn = self.connect()
            try:
                existing = dict((row["parameter_id"], dict(row)) for row in conn.execute("SELECT * FROM parameter_definitions"))
                specs = self.runtime.all_feature_specs()
                model_keys = set()
                for spec in specs:
                    key = spec["key"]
                    model_keys.add(key)
                    expected_type = "ip_grade" if spec.get("parser") == "ip_grade" else spec.get("dtype") or spec.get("type", "number")
                    if expected_type == "integer":
                        expected_type = "number"
                    current = existing.get(key)
                    if current is None:
                        warnings.append(
                            "模型Schema字段%s不在DataMaster指标定义中；该字段仍会按模型Schema缺省值参与计算，"
                            "如需在界面上编辑请先在指标定义中补充。" % key
                        )
                        continue
                    current_type = current.get("value_type")
                    if not model_types_compatible(current_type, expected_type):
                        warnings.append(
                            "指标%s的数据中心类型%s与模型类型%s不同；保留业务定义并在调用时尝试编码。" %
                            (key, current_type, expected_type)
                        )
                    if current.get("unit") not in (None, "") and spec.get("unit") not in (None, "") and current.get("unit") != spec.get("unit"):
                        warnings.append(
                            "指标%s的数据中心单位%s与模型单位%s不同；该差异不阻断API调用。" %
                            (key, current.get("unit"), spec.get("unit"))
                        )
                extras = sorted(set(existing) - model_keys)
                if extras:
                    warnings.append(
                        "以下DataMaster指标当前不在任何模型Schema中，将作为普通业务字段保留：%s" % "、".join(extras)
                    )
                self._sync_model_registry(conn)
                conn.commit()
            finally:
                conn.close()
        return warnings

    def _sync_model_registry(self, conn):
        manifest = self.runtime.manifest()
        if manifest.get("calculation_available") is False:
            # Preserve the last known registry as audit history.  A stopped HTTP
            # service is not a new local model installation.
            return
        for kind in ("effectiveness", "price"):
            item = manifest[kind]
            if manifest.get("execution_mode") == "independent_http_services":
                artifact_path = "service://%s/%s" % (item.get("backend") or "unknown", kind)
            else:
                artifact_path = "models/%s_bundle.json" % kind
            conn.execute("""INSERT OR REPLACE INTO model_registry
                (model_kind,model_version,product_code,artifact_path,artifact_sha256,contract_status,enabled,updated_at)
                VALUES(?,?,?,?,?,?,1,?)""", (
                kind, item["model_version"], manifest["product_code"], artifact_path,
                item.get("artifact_sha256"), "valid" if manifest.get("contract_valid") else "invalid", now_iso()
            ))

    def runtime_parameters(self, params):
        """Prepare business parameters for a model call.

        Field roles (shared / effectiveness-only / price-only) are derived from
        the two service schemas by ``parameter_roles`` and ``feature_roles``.
        Every business field is forwarded as-is; the target service owns field
        selection, parsing and missing-value policy. No operator-maintained
        "model field binding" table is consulted.
        """
        merged = dict(params or {})
        conn = self.connect()
        try:
            rows = conn.execute("SELECT parameter_id,model_value_mapping_json FROM parameter_definitions WHERE enabled=1").fetchall()
        finally:
            conn.close()
        for row in rows:
            key = row["parameter_id"]
            if key not in merged or merged[key] in (None, ""):
                continue
            mapping = _json_mapping(row["model_value_mapping_json"])
            if not mapping:
                continue
            lookup = str(merged[key]).strip()
            normalized = dict((str(source).strip().lower(), target) for source, target in mapping.items())
            if lookup.lower() in normalized:
                merged[key] = normalized[lookup.lower()]
        return merged

    def business_parameters(self, model_params, source_params=None):
        result = dict(source_params or {})
        conn = self.connect()
        try:
            rows = conn.execute("SELECT parameter_id,model_value_mapping_json FROM parameter_definitions WHERE enabled=1").fetchall()
        finally:
            conn.close()
        mappings = dict((row["parameter_id"], _json_mapping(row["model_value_mapping_json"])) for row in rows)
        for key, value in dict(model_params or {}).items():
            if key in result and result[key] not in (None, ""):
                continue
            reverse = {}
            for business, canonical in mappings.get(key, {}).items():
                reverse.setdefault(str(canonical).strip().lower(), business)
            result[key] = reverse.get(str(value).strip().lower(), value)
        return result

    def canonical_business_parameters(self, params):
        """Restore JSON form/select strings to DataMaster business scalar types."""
        result = dict(params or {})
        definitions = self.parameter_map()
        numeric_types = {"number", "float", "integer", "boolean", "bool", "ip_grade"}
        for key, value in list(result.items()):
            definition = definitions.get(key) or {}
            allowed = _json_list(definition.get("allowed_values_json"))
            matched = next((candidate for candidate in allowed if str(candidate) == str(value)), None)
            if matched is not None:
                result[key] = matched
                continue
            if str(definition.get("value_type") or "").lower() in numeric_types:
                number = _number(str(value).replace("IP", "").replace("ip", ""))
                if number is not None:
                    result[key] = int(number) if number.is_integer() else number
        return result

    def tag_map(self, include_disabled=False):
        conn = self.connect()
        try:
            sql = "SELECT * FROM tags" if include_disabled else "SELECT * FROM tags WHERE enabled=1"
            return dict((row["tag_id"], dict(row)) for row in conn.execute(sql))
        finally:
            conn.close()

    def tag_rule_rows(self):
        conn = self.connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM tag_rules WHERE enabled=1 ORDER BY tag_id,rule_group,rule_id")]
        finally:
            conn.close()

    def _tag_rule_match(self, values, rule, definitions):
        """Match one DataMaster tag rule, passing its parameter definition so mapped
        enums and boolean encodings use the same Phase 1 semantics as filtering."""
        return filter_match(values, {
            "parameter_id": rule.get("parameter_id"),
            "operator": rule.get("operator"),
            "value1": rule.get("value1"),
            "value2": rule.get("value2"),
        }, definitions.get(rule.get("parameter_id")))

    def derive_tags(self, params, evaluation=None, inherited_tags=None):
        """Derive generated-scheme tags from DataMaster rules, never code constants."""
        evaluation = evaluation or {}
        values = dict(params or {})
        values.update({
            "__predicted_price_wan": evaluation.get("predicted_price_wan"),
            "__capability_score": evaluation.get("capability_score"),
            "__feasibility_probability": evaluation.get("feasibility_probability"),
        })
        tags = self.tag_map()
        inherited = set(inherited_tags or [])
        definitions = self.parameter_map()
        grouped = {}
        for rule in self.tag_rule_rows():
            grouped.setdefault(rule["tag_id"], {}).setdefault(rule.get("rule_group") or "default", []).append(rule)
        result = []
        for tag_id, tag in tags.items():
            mode = str(tag.get("derivation_mode") or "rule").lower()
            if mode == "manual":
                # A manually confirmed tag cannot be re-derived from parameters,
                # but it is an established human fact: preserve it when inherited
                # from the base scheme, never silently drop it.
                if tag_id in inherited:
                    result.append(tag_id)
                continue
            if mode == "inherit":
                if tag_id in inherited:
                    result.append(tag_id)
                continue
            groups = grouped.get(tag_id, {})
            if not groups:
                continue
            matched = False
            for rules in groups.values():
                if all(self._tag_rule_match(values, rule, definitions) for rule in rules):
                    matched = True
                    break
            if matched:
                result.append(tag_id)
        return sorted(set(result))

    def tag_rule_branches(self, selected_tags, max_branches=24):
        """Compile selected tag OR-groups into explicit generation branches."""
        selected = [str(x) for x in (selected_tags or [])]
        tags = self.tag_map()
        grouped = {}
        for rule in self.tag_rule_rows():
            if rule.get("tag_id") in selected:
                grouped.setdefault(rule["tag_id"], {}).setdefault(rule.get("rule_group") or "default", []).append(rule)
        branches = [{"rules": [], "tag_groups": {}, "unresolved_tags": []}]
        for tag_id in selected:
            tag = tags.get(tag_id, {})
            mode = str(tag.get("derivation_mode") or "rule").lower()
            groups = grouped.get(tag_id, {}) if mode == "rule" else {}
            if not groups:
                for branch in branches:
                    branch["unresolved_tags"].append(tag_id)
                continue
            alternatives = []
            for group_name, rules in groups.items():
                alternatives.append({"tag_id": tag_id, "group": group_name, "rules": [dict(x) for x in rules]})
            expanded = []
            for branch in branches:
                for alternative in alternatives:
                    clone = {"rules": list(branch["rules"]), "tag_groups": dict(branch["tag_groups"]), "unresolved_tags": list(branch["unresolved_tags"])}
                    clone["rules"].extend(alternative["rules"])
                    clone["tag_groups"][tag_id] = alternative["group"]
                    expanded.append(clone)
                    if len(expanded) >= max_branches:
                        break
                if len(expanded) >= max_branches:
                    break
            branches = expanded or branches
        return branches or [{"rules": [], "tag_groups": {}, "unresolved_tags": selected}]

    def tag_evidence(self, params, evaluation=None, inherited_tags=None):
        evaluation = evaluation or {}
        values = dict(params or {})
        values.update({
            "__predicted_price_wan": evaluation.get("predicted_price_wan"),
            "__capability_score": evaluation.get("capability_score"),
            "__feasibility_probability": evaluation.get("feasibility_probability"),
        })
        tags = self.tag_map()
        inherited = set(inherited_tags or [])
        definitions = self.parameter_map()
        grouped = {}
        for rule in self.tag_rule_rows():
            grouped.setdefault(rule["tag_id"], {}).setdefault(rule.get("rule_group") or "default", []).append(rule)
        result = {}
        for tag_id, tag in tags.items():
            mode = str(tag.get("derivation_mode") or "rule").lower()
            entry = {"tag_id": tag_id, "tag_name": tag.get("tag_name", tag_id), "mode": mode, "matched": False, "status": "unmatched", "matched_group": None, "rules": []}
            if mode == "manual":
                entry["matched"] = tag_id in inherited
                entry["status"] = "manual_confirmed" if entry["matched"] else "expert_confirmation_required"
            elif mode == "inherit":
                entry["matched"] = tag_id in inherited
                entry["status"] = "inherited" if entry["matched"] else "not_inherited"
            else:
                for group_name, rules in grouped.get(tag_id, {}).items():
                    details = []
                    group_ok = True
                    for rule in rules:
                        match = self._tag_rule_match(values, rule, definitions)
                        details.append({"rule_id": rule.get("rule_id"), "parameter_id": rule.get("parameter_id"), "operator": rule.get("operator"), "value1": rule.get("value1"), "value2": rule.get("value2"), "matched": bool(match)})
                        group_ok = group_ok and bool(match)
                    if group_ok:
                        entry.update({"matched": True, "status": "rule_matched", "matched_group": group_name, "rules": details})
                        break
                    if not entry["rules"]:
                        entry["rules"] = details
            result[tag_id] = entry
        return result

    def tag_constraints(self, selected_tags):
        """Convert unambiguous DataMaster tag rules to direct parameter bounds."""
        selected = set(selected_tags or [])
        grouped = {}
        for rule in self.tag_rule_rows():
            if rule["tag_id"] in selected and not str(rule.get("parameter_id", "")).startswith("__"):
                grouped.setdefault(rule["tag_id"], {}).setdefault(rule.get("rule_group") or "default", []).append(rule)
        result = {}
        for tag_id, groups in grouped.items():
            # Multiple alternative groups are OR semantics and cannot safely be
            # projected to one interval. They remain strict post-generation gates.
            if len(groups) != 1:
                continue
            for rule in list(groups.values())[0]:
                key, op = rule.get("parameter_id"), rule.get("operator")
                target = result.setdefault(key, {})
                try:
                    v1 = float(str(rule.get("value1", "")).strip().upper().replace("IP", ""))
                    v2 = float(str(rule.get("value2", "")).strip().upper().replace("IP", "")) if rule.get("value2") not in (None, "") else None
                except (TypeError, ValueError):
                    if not target: result.pop(key, None)
                    continue
                if op in ("gte", "gt"): target["min"] = max(v1, float(target.get("min", v1)))
                elif op in ("lte", "lt"): target["max"] = min(v1, float(target.get("max", v1)))
                elif op in ("eq", "boolean_is", "special_is"):
                    target["min"] = v1; target["max"] = v1
                elif op == "range_inside" and v2 is not None:
                    target["min"] = min(v1, v2); target["max"] = max(v1, v2)
                else:
                    if not target: result.pop(key, None)
        return result

    def _positioning(self, tags):
        tag_map = self.tag_map()
        names = [tag_map[item]["tag_name"] for item in tags if item in tag_map]
        return "、".join(names[:4]) if names else "通用技术方案"

    def master_data_version(self):
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM metadata WHERE key='master_data_version'").fetchone()
            return row[0] if row else "0"
        finally:
            conn.close()

    def generation_semantics_fingerprint(self):
        """Hash generation-relevant business data, excluding presentation-only fields."""
        conn = self.connect()
        try:
            payload = {}
            for table in ("parameter_definitions", "parameter_groups", "tags", "tag_rules", "indicator_couplings", "constraint_rules", "agreements"):
                rows = [dict(row) for row in conn.execute("SELECT * FROM %s ORDER BY 1" % table)]
                if table == "parameter_definitions":
                    for row in rows:
                        row.pop("display_value_mapping_json", None)
                payload[table] = rows
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
            return hashlib.sha256(raw.encode("utf-8")).hexdigest()
        finally:
            conn.close()

    def _audit(self, conn, action, object_type, object_id, detail=None):
        conn.execute("INSERT INTO audit_log(action,object_type,object_id,detail_json,created_at) VALUES(?,?,?,?,?)", (
            action, object_type, object_id, json.dumps(detail or {}, ensure_ascii=False), now_iso()
        ))

    def parameter_roles(self):
        roles = self.runtime.feature_roles()
        shared = set(roles.get("shared_features") or [])
        effect_only = set(roles.get("effectiveness_only_features") or [])
        price_only = set(roles.get("price_only_features") or [])
        result = {}
        for spec in self.runtime.all_feature_specs():
            key = spec["key"]
            role = "shared" if key in shared else "price_only" if key in price_only else "effectiveness_only"
            result[key] = {
                "parameter_id": key,
                "model_role": role,
                "affects_effectiveness": role in ("shared", "effectiveness_only"),
                "affects_price": role in ("shared", "price_only"),
                "default_visible": role != "price_only",
                "editable": bool(spec.get("editable", True)),
                "default_value": spec.get("default_value") if spec.get("default_value") is not None else spec.get("training_mean"),
            }
        return result

    def bootstrap(self):
        conn = self.connect()
        try:
            product_row = conn.execute("SELECT * FROM products WHERE product_code=? AND enabled=1 LIMIT 1", (self.current_product_code(),)).fetchone()
            product = dict(product_row) if product_row else {}
            parameters = [dict(row) for row in conn.execute("SELECT * FROM parameter_definitions WHERE enabled=1 ORDER BY display_order,label")]
            role_map = self.parameter_roles()
            for item in parameters:
                item.update(role_map.get(item["parameter_id"], {"model_role":"effectiveness_only","affects_effectiveness":True,"affects_price":False,"default_visible":True,"editable":True,"default_value":None}))
            tags = [dict(row) for row in conn.execute("SELECT * FROM tags WHERE enabled=1 ORDER BY tag_group,tag_name")]
            parameter_groups = [dict(row) for row in conn.execute("SELECT * FROM parameter_groups ORDER BY display_order, group_name")]
            models = [dict(row) for row in conn.execute("SELECT * FROM model_registry ORDER BY model_kind")]
            counts = {
                "historical": conn.execute("SELECT COUNT(*) FROM agreements WHERE agreement_source IN ('historical','imported') AND enabled=1").fetchone()[0],
                "static_generated": conn.execute("SELECT COUNT(*) FROM agreements WHERE agreement_source IN ('live_generated','generated_model','generated')").fetchone()[0],
                "saved": conn.execute("SELECT COUNT(*) FROM saved_schemes").fetchone()[0],
            }
            version_row = conn.execute("SELECT value FROM metadata WHERE key='master_data_version'").fetchone()
            return {"product": product, "parameters": parameters, "parameter_roles": role_map,
                    "tags": tags, "parameter_groups": parameter_groups, "models": models,
                    "counts": counts, "master_data_version": version_row[0] if version_row else "0"}
        finally:
            conn.close()

    def parameter_map(self):
        conn = self.connect()
        try:
            return dict((row["parameter_id"], dict(row)) for row in conn.execute("SELECT * FROM parameter_definitions"))
        finally:
            conn.close()

    def current_product_code(self):
        conn = self.connect()
        try:
            row = conn.execute("SELECT product_code FROM products ORDER BY enabled DESC, product_code LIMIT 1").fetchone()
            return str(row["product_code"]) if row else ""
        finally:
            conn.close()

    def historical_agreements(self, target_protocol=None, recalculate=True):
        conn = self.connect()
        try:
            product_code = self.current_product_code()
            rows = conn.execute("SELECT * FROM agreements WHERE product_code=? AND enabled=1 ORDER BY agreement_id", (product_code,)).fetchall()
            items = [self._agreement_row(row, recalculate=False) for row in rows]
        finally:
            conn.close()
        if not items or not recalculate:
            return items
        requests = [
            {
                "candidate_id": item["agreement_id"],
                "parameters": self.runtime_parameters(item["params"]),
                "target_protocol": target_protocol,
                "historical_price_wan": item.get("historical_price_wan"),
            }
            for item in items
        ]
        try:
            if hasattr(self.runtime, "evaluate_batch_effectiveness_only"):
                # Unchanged historical samples keep their stored transaction price;
                # only effectiveness is re-evaluated against the current model.
                evaluations = self.runtime.evaluate_batch_effectiveness_only(requests, target_protocol=target_protocol)
            elif hasattr(self.runtime, "evaluate_batch"):
                evaluations = self.runtime.evaluate_batch(requests, target_protocol=target_protocol)
            else:
                evaluations = [
                    self._evaluate_historical_one(
                        request["parameters"], target_protocol=target_protocol,
                        historical_price_wan=request.get("historical_price_wan"),
                    )
                    for request in requests
                ]
            if len(evaluations) != len(items):
                raise ValueError("历史成品批量评价返回数量与请求数量不一致。")
            return [self._apply_agreement_evaluation(item, evaluation) for item, evaluation in zip(items, evaluations)]
        except Exception as batch_error:
            # One incomplete legacy row must not hide the rest of the historical
            # catalog. Retry independently and keep failures as model-free rows.
            result = []
            for item, request in zip(items, requests):
                try:
                    evaluation = self._evaluate_historical_one(
                        request["parameters"], target_protocol=target_protocol,
                        historical_price_wan=request.get("historical_price_wan"),
                    )
                    result.append(self._apply_agreement_evaluation(item, evaluation))
                except Exception as exc:
                    preserved = dict(item)
                    preserved["model_evaluation_available"] = False
                    preserved["model_evaluation_error"] = str(exc)
                    preserved["batch_evaluation_error"] = str(batch_error)
                    result.append(preserved)
            return result

    def workbench_example(self, preferred_agreement_id=None):
        """Return one deterministic historical business-value example."""
        conn = self.connect()
        try:
            product_code = self.current_product_code()
            row = None
            if preferred_agreement_id:
                row = conn.execute(
                    """SELECT * FROM agreements WHERE product_code=? AND agreement_id=?
                       AND enabled=1 AND agreement_source IN ('historical','imported') LIMIT 1""",
                    (product_code, str(preferred_agreement_id)),
                ).fetchone()
            if row is None:
                row = conn.execute(
                    """SELECT * FROM agreements WHERE product_code=? AND enabled=1
                       AND agreement_source IN ('historical','imported')
                       ORDER BY CASE WHEN source_year IS NULL THEN 1 ELSE 0 END,
                                source_year DESC, updated_at DESC, agreement_id ASC LIMIT 1""",
                    (product_code,),
                ).fetchone()
            if row is None:
                return None
            item = self._agreement_row(row, recalculate=False)
            return {
                "agreement_id": item.get("agreement_id"),
                "agreement_name": item.get("agreement_name"),
                "source_year": item.get("source_year"),
                "parameters": dict(item.get("params") or {}),
            }
        finally:
            conn.close()

    def _evaluate_historical_one(self, params, target_protocol=None, historical_price_wan=None):
        """Evaluate one unchanged historical sample (effectiveness-only when available)."""
        if hasattr(self.runtime, "evaluate_effectiveness_only"):
            return self.runtime.evaluate_effectiveness_only(
                params, target_protocol=target_protocol, historical_price_wan=historical_price_wan,
            )
        if target_protocol not in (None, ""):
            return self.runtime.evaluate(params, target_protocol=target_protocol)
        return self.runtime.evaluate(params)

    def historical_boundary_profile(self):
        """Return a model-free historical envelope for generation preflight.

        This deliberately reads stored business data only, so the UI can warn
        about a deep extrapolation immediately without waiting for either model
        service.  The generator still uses freshly evaluated history for search.
        """
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT historical_price_wan,capability_score,feasibility_probability,params_json "
                "FROM agreements WHERE product_code=? AND enabled=1",
                (self.current_product_code(),),
            ).fetchall()
        finally:
            conn.close()
        prices, capabilities, feasibilities = [], [], []
        attributes = {}
        for row in rows:
            for target, key in (
                (prices, "historical_price_wan"),
                (capabilities, "capability_score"),
                (feasibilities, "feasibility_probability"),
            ):
                value = _number(row[key])
                if value is not None:
                    target.append(value)
            try:
                params = json.loads(row["params_json"] or "{}")
            except Exception:
                params = {}
            for key, raw in params.items():
                value = _number(raw)
                if value is not None:
                    attributes.setdefault(str(key), []).append(value)
        def envelope(values):
            return [min(values), max(values)] if values else None
        return {
            "sample_count": len(rows),
            "price_wan": envelope(prices),
            "capability_score": envelope(capabilities),
            "feasibility_probability": envelope(feasibilities),
            "attributes": dict((key, envelope(values)) for key, values in attributes.items() if values),
        }

    def get_historical(self, agreement_id, target_protocol=None, recalculate=True):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)).fetchone()
            return self._agreement_row(row, recalculate=recalculate, target_protocol=target_protocol) if row else None
        finally:
            conn.close()

    def _agreement_row(self, row, recalculate=False, target_protocol=None):
        item = dict(row)
        item["params"] = _json_object(item.pop("params_json"))
        item["tags"] = _json_list(item.pop("tags_json"))
        item["is_generated"] = item["agreement_source"] in ("live_generated", "generated_model", "generated")
        if recalculate:
            runtime_params = self.runtime_parameters(item["params"])
            evaluation = self._evaluate_historical_one(
                runtime_params, target_protocol=target_protocol,
                historical_price_wan=item.get("historical_price_wan"),
            )
            item = self._apply_agreement_evaluation(item, evaluation)
        return item

    def _apply_agreement_evaluation(self, item, evaluation):
        model_parameters = dict(evaluation.get("parameters") or {})
        evaluation["model_parameters"] = model_parameters
        evaluation["parameters"] = self.business_parameters(model_parameters, item["params"])
        item["params"] = dict(evaluation["parameters"])
        item["predicted_price_wan"] = evaluation["predicted_price_wan"]
        item["price_interval_wan"] = evaluation["price_interval_wan"]
        item["capability_score"] = evaluation["capability_score"]
        item["conservative_capability_score"] = evaluation.get("conservative_capability_score", evaluation["capability_score"])
        item["protocol_score_interval"] = evaluation.get("protocol_score_interval")
        item["support_at_80"] = evaluation.get("support_at_80")
        item["support_at_100"] = evaluation.get("support_at_100")
        item["score_uncertainty_width"] = evaluation.get("score_uncertainty_width")
        item["feasibility_probability"] = evaluation["feasibility_probability"]
        item["physical_gate"] = evaluation.get("physical_gate") or {}
        item["cost_effectiveness"] = evaluation["cost_effectiveness"]
        item["price_source"] = evaluation.get("price_source", "predicted")
        item["evaluation"] = evaluation
        item["model_evaluation_available"] = True
        item["tags"] = self.derive_tags(item["params"], evaluation, item.get("tags") or [])
        return item

    def save_scheme(self, scheme_name, base_agreement_id, source_type, params, evaluation,
                    risk_confirmed=False, base_params=None, delta=None,
                    changed_parameter_ids=None, target_protocol=None,
                    schema_signature=None, recommendation_eligible=True,
                    training_candidate=True, base_scheme_name=None):
        with self.lock:
            conn = self.connect()
            try:
                # Keep old customer databases and lightweight test fixtures
                # forward-compatible with the V21.5.2 lineage fields.
                self._add_column(conn, "saved_schemes", "expert_revision_no INTEGER")
                self._add_column(conn, "saved_schemes", "root_base_agreement_id TEXT")
                self._add_column(conn, "saved_schemes", "parent_saved_scheme_id INTEGER")
                raw_base = str(base_agreement_id or "").strip()
                parent_saved_scheme_id = None
                root_base_agreement_id = raw_base
                if raw_base.upper().startswith("SAVED-"):
                    try:
                        parent_saved_scheme_id = int(raw_base.split("-", 1)[1])
                    except (TypeError, ValueError):
                        parent_saved_scheme_id = None
                if parent_saved_scheme_id is not None:
                    parent = conn.execute(
                        "SELECT root_base_agreement_id,base_agreement_id,scheme_name FROM saved_schemes WHERE id=?",
                        (parent_saved_scheme_id,),
                    ).fetchone()
                    if parent:
                        root_base_agreement_id = str(parent["root_base_agreement_id"] or parent["base_agreement_id"] or raw_base)
                        if not base_scheme_name:
                            base_scheme_name = parent["scheme_name"]
                product_code = self.current_product_code()
                revision_row = conn.execute(
                    "SELECT COALESCE(MAX(expert_revision_no),0) FROM saved_schemes "
                    "WHERE product_code=? AND root_base_agreement_id=?",
                    (product_code, root_base_agreement_id),
                ).fetchone()
                expert_revision_no = int(revision_row[0] or 0) + 1
                final_name = str(scheme_name or "").strip()
                if not final_name:
                    base_label = str(base_scheme_name or root_base_agreement_id or "专家方案").strip()
                    # Continuing an expert scheme should retain its original
                    # root label rather than stacking '-专家修订' repeatedly.
                    marker = "-专家修订-"
                    if marker in base_label:
                        base_label = base_label.split(marker, 1)[0]
                    final_name = "%s-专家修订-%02d" % (base_label, expert_revision_no)
                cursor = conn.execute("""INSERT INTO saved_schemes
                    (scheme_name,base_agreement_id,product_code,source_type,params_json,evaluation_json,risk_confirmed,created_at,
                     base_params_json,delta_json,changed_parameter_ids_json,target_protocol,schema_signature,
                     recommendation_eligible,training_candidate,enabled,expert_revision_no,root_base_agreement_id,parent_saved_scheme_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)""", (
                    final_name, base_agreement_id, product_code, source_type, json.dumps(params, ensure_ascii=False),
                    json.dumps(evaluation, ensure_ascii=False), 1 if risk_confirmed else 0, now_iso(),
                    json.dumps(base_params, ensure_ascii=False) if base_params is not None else None,
                    json.dumps(delta or {}, ensure_ascii=False),
                    json.dumps(changed_parameter_ids or [], ensure_ascii=False), target_protocol, schema_signature,
                    1 if recommendation_eligible else 0, 1 if training_candidate else 0,
                    expert_revision_no, root_base_agreement_id, parent_saved_scheme_id,
                ))
                self._audit(conn, "create", "saved_scheme", str(cursor.lastrowid), {
                    "scheme_name": final_name, "base_agreement_id": base_agreement_id,
                    "root_base_agreement_id": root_base_agreement_id,
                    "parent_saved_scheme_id": parent_saved_scheme_id,
                    "expert_revision_no": expert_revision_no,
                    "changed_parameter_ids": list(changed_parameter_ids or []),
                    "changed_count": len(changed_parameter_ids or []),
                    "product_code": self.current_product_code(),
                    "recommendation_eligible": bool(recommendation_eligible),
                    "training_candidate": bool(training_candidate),
                })
                conn.commit(); return cursor.lastrowid
            finally: conn.close()

    def list_saved(self):
        conn = self.connect()
        try:
            result = []
            for row in conn.execute("SELECT * FROM saved_schemes ORDER BY id DESC LIMIT 200"):
                item = dict(row)
                item["params"] = _json_object(item.pop("params_json"))
                item["evaluation"] = _json_object(item.pop("evaluation_json"))
                item["base_params"] = _json_object(item.pop("base_params_json", None))
                item["delta"] = _json_object(item.pop("delta_json", None))
                item["changed_parameter_ids"] = _json_list(item.pop("changed_parameter_ids_json", None))
                result.append(item)
            return result
        finally: conn.close()

    def get_saved(self, scheme_id, recalculate=False, target_protocol=None):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM saved_schemes WHERE id=?", (int(scheme_id),)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["params"] = _json_object(item.pop("params_json"))
            item["saved_evaluation"] = _json_object(item.pop("evaluation_json"))
            item["base_params"] = _json_object(item.pop("base_params_json", None))
            item["delta"] = _json_object(item.pop("delta_json", None))
            item["changed_parameter_ids"] = _json_list(item.pop("changed_parameter_ids_json", None))
            if recalculate:
                model_input = self.runtime_parameters(item["params"])
                item["evaluation"] = (
                    self.runtime.evaluate(model_input, target_protocol=target_protocol)
                    if target_protocol not in (None, "") else self.runtime.evaluate(model_input)
                )
            else:
                item["evaluation"] = item["saved_evaluation"]
            if recalculate:
                model_parameters = dict(item["evaluation"].get("parameters") or {})
                item["evaluation"]["model_parameters"] = model_parameters
                item["evaluation"]["parameters"] = self.business_parameters(model_parameters, item["params"])
                item["params"] = dict(item["evaluation"]["parameters"])
            item["agreement_id"] = "SAVED-%s" % item["id"]
            item["agreement_name"] = item["scheme_name"]
            item["agreement_source"] = "expert_saved"
            item["is_generated"] = False
            item["positioning"] = "专家保存方案"
            item["tags"] = self.derive_tags(item["params"], item["evaluation"])
            item["predicted_price_wan"] = item["evaluation"].get("predicted_price_wan")
            item["capability_score"] = item["evaluation"].get("capability_score")
            item["conservative_capability_score"] = item["evaluation"].get("conservative_capability_score", item["capability_score"])
            item["feasibility_probability"] = item["evaluation"].get("feasibility_probability")
            item["cost_effectiveness"] = item["evaluation"].get("cost_effectiveness")
            item["model_evaluation_available"] = bool(recalculate)
            return item
        finally:
            conn.close()

    def set_saved_recommendation_eligibility(self, scheme_id, enabled):
        with self.lock:
            conn = self.connect()
            try:
                cursor = conn.execute(
                    "UPDATE saved_schemes SET recommendation_eligible=? WHERE id=? AND enabled=1",
                    (1 if enabled else 0, int(scheme_id)),
                )
                if not cursor.rowcount:
                    raise ValueError("专家方案不存在或已停用。")
                self._audit(conn, "recommendation_eligibility", "saved_scheme", str(scheme_id), {"enabled": bool(enabled)})
                conn.commit()
                return {"updated": True, "id": int(scheme_id), "recommendation_eligible": bool(enabled)}
            finally:
                conn.close()

    @staticmethod
    def _migrate_saved_scheme_lineage(conn):
        """Backfill stable lineage for databases created before V21.5.2."""
        rows = conn.execute(
            "SELECT id,product_code,base_agreement_id,root_base_agreement_id,"
            "parent_saved_scheme_id,expert_revision_no FROM saved_schemes ORDER BY id"
        ).fetchall()
        roots, counters = {}, {}
        for row in rows:
            raw_base = str(row["base_agreement_id"] or "")
            parent_id = row["parent_saved_scheme_id"]
            if parent_id is None and raw_base.upper().startswith("SAVED-"):
                try:
                    parent_id = int(raw_base.split("-", 1)[1])
                except (TypeError, ValueError):
                    parent_id = None
            root = str(row["root_base_agreement_id"] or "")
            if not root:
                root = roots.get(parent_id) or raw_base
            key = (str(row["product_code"] or ""), root)
            revision = row["expert_revision_no"]
            if revision is None:
                revision = counters.get(key, 0) + 1
            counters[key] = max(counters.get(key, 0), int(revision))
            roots[int(row["id"])] = root
            conn.execute(
                "UPDATE saved_schemes SET root_base_agreement_id=?,parent_saved_scheme_id=?,expert_revision_no=? WHERE id=?",
                (root, parent_id, int(revision), int(row["id"])),
            )

    def set_saved_training_candidate(self, scheme_id, enabled):
        with self.lock:
            conn = self.connect()
            try:
                cursor = conn.execute(
                    "UPDATE saved_schemes SET training_candidate=? WHERE id=? AND enabled=1",
                    (1 if enabled else 0, int(scheme_id)),
                )
                if not cursor.rowcount:
                    raise ValueError("专家方案不存在或已停用。")
                self._audit(conn, "training_candidate", "saved_scheme", str(scheme_id), {"enabled": bool(enabled)})
                conn.commit()
                return {"updated": True, "id": int(scheme_id), "training_candidate": bool(enabled)}
            finally:
                conn.close()

    def audit_event(self, action, object_type, object_id, detail=None):
        with self.lock:
            conn = self.connect()
            try:
                self._audit(conn, action, object_type, object_id, detail or {})
                conn.commit()
            finally:
                conn.close()

    def saved_by_ids(self, scheme_ids):
        wanted = set(int(value) for value in (scheme_ids or []))
        return [item for item in self.list_saved() if int(item.get("id")) in wanted]

    def import_wide_rows(self, parsed_rows, overwrite=False, evaluate=True):
        valid = [entry["item"] for entry in parsed_rows if entry.get("valid")]
        if not valid:
            raise ValueError("没有可导入的有效宽表记录。")
        inserted, updated = 0, 0
        with self.lock:
            conn = self.connect()
            try:
                for item in valid:
                    exists = conn.execute("SELECT 1 FROM agreements WHERE agreement_id=?", (item["agreement_id"],)).fetchone()
                    if exists and not overwrite:
                        raise ValueError("协议编号%s已存在；请启用覆盖模式或修改编号。" % item["agreement_id"])
                    if evaluate:
                        evaluation = self.runtime.evaluate(self.runtime_parameters(item["params"]))
                        stored_params = evaluation.get("parameters") or item["params"]
                        tags = item.get("tags") or self.derive_tags(stored_params, evaluation)
                        capability_score = evaluation.get("capability_score")
                        feasibility_probability = evaluation.get("feasibility_probability")
                    else:
                        # Business-data staging/activation must not call whichever
                        # HTTP model happens to be running at the moment.
                        stored_params = dict(item.get("params") or {})
                        tags = list(item.get("tags") or [])
                        capability_score = None
                        feasibility_probability = None
                    values = (
                        item["agreement_id"], item.get("product_code") or self.current_product_code(), item["agreement_name"],
                        item.get("positioning") or self._positioning(tags), item.get("agreement_source") or "imported",
                        item.get("source_year"), item.get("supplier_type"), item.get("historical_price_wan"),
                        capability_score, feasibility_probability,
                        json.dumps(stored_params, ensure_ascii=False), json.dumps(tags, ensure_ascii=False),
                        int(item.get("enabled", 1)), now_iso()
                    )
                    conn.execute("""INSERT INTO agreements
                        (agreement_id,product_code,agreement_name,positioning,agreement_source,source_year,supplier_type,
                         historical_price_wan,capability_score,feasibility_probability,params_json,tags_json,enabled,updated_at)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(agreement_id) DO UPDATE SET
                         product_code=excluded.product_code,agreement_name=excluded.agreement_name,positioning=excluded.positioning,
                         agreement_source=excluded.agreement_source,source_year=excluded.source_year,supplier_type=excluded.supplier_type,
                         historical_price_wan=excluded.historical_price_wan,capability_score=excluded.capability_score,
                         feasibility_probability=excluded.feasibility_probability,params_json=excluded.params_json,
                         tags_json=excluded.tags_json,enabled=excluded.enabled,updated_at=excluded.updated_at""", values)
                    updated += 1 if exists else 0; inserted += 0 if exists else 1
                    self._audit(conn, "wide_import_update" if exists else "wide_import_create", "agreement", item["agreement_id"], {"source": "wide_table", "evaluated": bool(evaluate)})
                conn.commit()
            finally:
                conn.close()
        return {"imported": True, "inserted": inserted, "updated": updated, "total": inserted + updated}

    def replace_from_datamaster(self, data, evaluate_agreements=True, sync_model_contract=True):
        """Atomically replace operator-maintained business data.

        Model-service calls and model-binding synchronization are optional so a
        new product can be installed before or after its independent HTTP model
        services. Runtime readiness is handled outside this transaction.
        """
        if self.read_only:
            raise ValueError("只读演示模式不能提交DataMaster。")
        backup = self.create_backup("before_datamaster") if self.db_path.exists() else None
        version = datetime.now().strftime("%Y%m%d%H%M%S")
        with self.lock:
            conn = self.connect()
            try:
                conn.execute("BEGIN IMMEDIATE")
                for table in ("agreements", "constraint_rules", "indicator_couplings", "tag_rules", "tags", "parameter_definitions", "parameter_groups", "products"):
                    conn.execute("DELETE FROM %s" % table)
                for item in data.get("products", []):
                    conn.execute("INSERT INTO products(product_code,product_name,product_description,enabled) VALUES(?,?,?,?)", (item["product_code"],item["product_name"],item.get("product_description"),int(item.get("enabled",1))))
                for item in data.get("parameters", []):
                    special_values = _json_list(item.get("special_value_keys_json"))
                    display_mapping = dump_display_mapping(
                        item.get("display_value_mapping_json"),
                        _json_list(item.get("allowed_values_json")) + special_values,
                    )
                    special_keys = item.get("special_value_keys_json")
                    if isinstance(special_keys, (list, tuple)):
                        special_keys = json.dumps([str(value) for value in special_keys], ensure_ascii=False)
                    conn.execute("""INSERT INTO parameter_definitions
                        (parameter_id,label,parameter_group,unit,value_type,min_value,max_value,observed_min,observed_max,preference,description,adjustment_hint,
                         allowed_values_json,model_value_mapping_json,display_value_mapping_json,special_value_keys_json,search_type,required,auto_adjustable,decimal_places,display_order,enabled,model_bound)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                        item["parameter_id"],item["label"],item.get("parameter_group") or "其他",item.get("unit"),item["value_type"],item.get("min_value"),item.get("max_value"),item.get("observed_min"),item.get("observed_max"),item.get("preference"),item.get("description"),item.get("adjustment_hint"),item.get("allowed_values_json"),item.get("model_value_mapping_json"),display_mapping,special_keys or None,item.get("search_type") or "auto",int(item.get("required",1)),int(item.get("auto_adjustable",1)),int(item.get("decimal_places",3)),int(item.get("display_order",1)),int(item.get("enabled",1)),int(item.get("model_bound",1))))
                group_items = data.get("parameter_groups")
                if not group_items:
                    seen = []
                    for p in data.get("parameters", []):
                        g = p.get("parameter_group") or "其他"
                        if g not in seen:
                            seen.append(g)
                    if "其他" not in seen:
                        seen.append("其他")
                    group_items = [{"group_name": g, "display_order": i + 1, "description": "", "enabled": 1, "default_collapsed": 0} for i, g in enumerate(seen)]
                seen_group_names = set()
                for item in group_items:
                    name = item.get("group_name") or "其他"
                    if name in seen_group_names:
                        continue
                    seen_group_names.add(name)
                    conn.execute(
                        "INSERT INTO parameter_groups(group_name,display_order,description,enabled,default_collapsed) VALUES(?,?,?,?,?)",
                        (name, int(item.get("display_order", 9999) or 9999), item.get("description") or "",
                         int(item.get("enabled", 1) or 0), int(item.get("default_collapsed", 0) or 0))
                    )
                for p in data.get("parameters", []):
                    g = p.get("parameter_group") or "其他"
                    if g not in seen_group_names:
                        conn.execute(
                            "INSERT OR IGNORE INTO parameter_groups(group_name,display_order,description,enabled,default_collapsed) VALUES(?,?,?,1,0)",
                            (g, 9999, "")
                        )
                        seen_group_names.add(g)
                for item in data.get("tags", []):
                    conn.execute("INSERT INTO tags(tag_id,tag_name,tag_group,weight,derivation_mode,description,enabled) VALUES(?,?,?,?,?,?,?)", (item["tag_id"],item["tag_name"],item.get("tag_group"),float(item.get("weight",1)),item.get("derivation_mode") or "rule",item.get("description"),int(item.get("enabled",1))))
                for item in data.get("tag_rules", []):
                    conn.execute("INSERT INTO tag_rules(rule_id,tag_id,parameter_id,operator,value1,value2,rule_group,enabled) VALUES(?,?,?,?,?,?,?,?)", (item["rule_id"],item["tag_id"],item["parameter_id"],item["operator"],item.get("value1"),item.get("value2"),item.get("rule_group") or "default",int(item.get("enabled",1))))
                for item in data.get("couplings", []):
                    conn.execute("""INSERT INTO indicator_couplings
                        (coupling_id,coupling_name,coupling_type,parameter_a,parameter_b,domain_operator,multiplier,offset,strength,severity,description,rationale,display_order,enabled)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (item["coupling_id"],item["coupling_name"],item["coupling_type"],item["parameter_a"],item["parameter_b"],item.get("domain_operator"),item.get("multiplier"),item.get("offset"),item.get("strength"),item.get("severity"),item.get("description"),item.get("rationale"),int(item.get("display_order",1)),int(item.get("enabled",1))))
                for item in data.get("constraints", []):
                    conn.execute("""INSERT INTO constraint_rules
                        (rule_id,rule_name,left_parameter,operator,right_parameter,multiplier,offset,severity,message,rationale,display_order,enabled,rule_kind,constraint_group,template_metadata_json)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (item["rule_id"],item["rule_name"],item["left_parameter"],item["operator"],item.get("right_parameter"),float(item.get("multiplier",1)),float(item.get("offset",0)),item.get("severity"),item.get("message"),item.get("rationale"),int(item.get("display_order",1)),int(item.get("enabled",1)),item.get("rule_kind") or "affine",item.get("constraint_group"),item.get("template_metadata_json")))
                if sync_model_contract:
                    # Runtime contract rows describe external model services and
                    # are not part of the business DataMaster authority.
                    self._sync_model_registry(conn)
                conn.execute("INSERT OR REPLACE INTO metadata(key,value) VALUES('master_data_version',?)", (version,))
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()
        # Agreements require model evaluation. If any unexpected runtime error
        # occurs after master tables were committed, restore the pre-import backup
        # so users never end up with a half-applied DataMaster.
        active_product_code = (
            (data.get("products") or [{}])[0].get("product_code") or self.current_product_code()
        )
        protocol_rows = []
        for source_item in data.get("agreements", []):
            item = dict(source_item)
            if not item.get("product_code"):
                item["product_code"] = active_product_code
            protocol_rows.append({"item": item, "valid": True})
        try:
            result = self.import_wide_rows(
                protocol_rows, overwrite=True, evaluate=evaluate_agreements,
            ) if protocol_rows else {"inserted":0,"updated":0,"total":0}
        except Exception:
            if backup:
                source = self.backup_dir / backup["name"]
                with self.lock:
                    temp = self.db_path.with_suffix(".datamaster_rollback.tmp")
                    shutil.copy2(str(source), str(temp))
                    for suffix in ("-wal", "-shm"):
                        sidecar = Path(str(self.db_path) + suffix)
                        if sidecar.exists(): sidecar.unlink()
                    os.replace(str(temp), str(self.db_path))
                self._initialize()
            raise
        conn = self.connect()
        try:
            self._audit(conn,"replace","datamaster",version,{"counts":dict((k,len(v)) for k,v in data.items() if isinstance(v,list))})
            conn.commit()
        finally:
            conn.close()
        return {
            "committed":True,"master_data_version":version,"backup":backup and backup.get("name"),
            "agreements":result,"agreements_evaluated":bool(evaluate_agreements),
            "model_contract_synced":bool(sync_model_contract),
        }

    def coupling_rows(self):
        conn = self.connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM indicator_couplings WHERE enabled=1 ORDER BY display_order")]
        finally:
            conn.close()

    def constraint_rows(self):
        conn = self.connect()
        try:
            return [dict(row) for row in conn.execute("SELECT * FROM constraint_rules WHERE enabled=1 ORDER BY display_order")]
        finally:
            conn.close()

    def upsert_conditional_template(self, payload):
        """Create or replace a conditional-attribute template as one atomic group."""
        from .conditional_constraint import build_rule_id, compile_conditional_constraint, compile_conditional_relationship_v2
        payload = dict(payload or {})
        controller = str(payload.get("controller") or "").strip()
        target = str(payload.get("target") or "").strip()
        if not controller or not target:
            raise ValueError("请选择控制指标和从属指标。")
        parameter_ids = set(self.parameter_map())
        if controller not in parameter_ids:
            raise ValueError("控制指标不存在：%s" % controller)
        if target not in parameter_ids:
            raise ValueError("从属指标不存在：%s" % target)
        active_value = payload.get("active_value")
        if active_value in (None, ""):
            active_value = 1
        if payload.get("template") == "conditional_applicability_v2" or (payload.get("then") is not None and payload.get("otherwise") is not None):
            compiled = compile_conditional_relationship_v2(
                controller,
                payload.get("when") or {},
                target,
                payload.get("then") or {},
                payload.get("otherwise") or {},
            )
        else:
            compiled = compile_conditional_constraint(
                controller, active_value, target,
                payload.get("inactive_value", -1),
                payload.get("active_min", 0),
                payload.get("active_max", 1),
            )
        from .conditional_compatibility import validate_conditional_relationship
        if hasattr(self.runtime, "model_feature_specs"):
            model_specs = self.runtime.model_feature_specs()
        elif hasattr(self.runtime, "all_feature_specs"):
            model_specs = self.runtime.all_feature_specs()
        else:
            model_specs = []
        compatibility = validate_conditional_relationship(compiled["template_metadata"], self.parameter_map(), model_specs)
        if compatibility["business_errors"]:
            raise ValueError("；".join(compatibility["business_errors"]))
        group = compiled["constraint_group"]
        original_group = str(payload.get("original_constraint_group") or "").strip() or None
        severity = payload.get("severity") or "warning"
        message = payload.get("message") or "控制条件不满足时，该从属指标应取不适用值。"
        rationale = payload.get("rationale") or ""
        display_order = int(payload.get("display_order") or 1)
        with self.lock:
            conn = self.connect()
            try:
                # Edit = replace the whole group, never leave a dangling half-rule.
                # A controller/target edit produces a new group id, so also drop the
                # previous group.
                conn.execute("DELETE FROM constraint_rules WHERE constraint_group=?", (group,))
                if original_group and original_group != group:
                    conn.execute("DELETE FROM constraint_rules WHERE constraint_group=?", (original_group,))
                for index, rule in enumerate(compiled["rules"]):
                    rule_id = build_rule_id(group, rule["rule_kind"])
                    rule_name = "%s %s" % (target, "下界" if rule["rule_kind"] == "conditional_lower" else "上界")
                    conn.execute(
                        "INSERT INTO constraint_rules(rule_id,rule_name,left_parameter,operator,right_parameter,"
                        "multiplier,offset,severity,message,rationale,display_order,enabled,"
                        "rule_kind,constraint_group,template_metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (rule_id, rule_name, rule["left_parameter"], rule["operator"], rule["right_parameter"],
                         rule["multiplier"], rule["offset"], severity, message, rationale,
                         display_order + index, 1, rule["rule_kind"], group, rule["template_metadata_json"]))
                self._audit(conn, "upsert", "constraints", group, {"conditional_template": compiled["template_metadata"]})
                conn.commit()
            finally:
                conn.close()
        return {"saved": True, "constraint_group": group, "rules": len(compiled["rules"]), "template_metadata": compiled["template_metadata"], "compatibility": compatibility}

    def delete_conditional_template(self, constraint_group):
        with self.lock:
            conn = self.connect()
            try:
                cur = conn.execute("DELETE FROM constraint_rules WHERE constraint_group=?", (str(constraint_group),))
                self._audit(conn, "delete", "constraints", str(constraint_group), {})
                conn.commit()
                return {"deleted": cur.rowcount}
            finally:
                conn.close()

    def conditional_templates(self):
        conn = self.connect()
        try:
            rows = [dict(row) for row in conn.execute(
                "SELECT * FROM constraint_rules WHERE constraint_group IS NOT NULL AND enabled=1 ORDER BY display_order")]
        finally:
            conn.close()
        groups = {}
        for row in rows:
            raw = row.get("template_metadata_json")
            meta = json.loads(raw) if raw else {}
            group = groups.setdefault(row["constraint_group"], {
                "constraint_group": row["constraint_group"], "template_metadata": meta, "rules": []})
            group["rules"].append({
                "rule_id": row["rule_id"], "rule_kind": row["rule_kind"],
                "operator": row["operator"], "multiplier": row["multiplier"], "offset": row["offset"],
                "constraint_group": row["constraint_group"],
                "template_metadata_json": row.get("template_metadata_json")})
        return list(groups.values())

    # -------------------------- Rule assessment --------------------------
    @staticmethod
    def _compare(left, operator, right):
        if operator == "gt": return left > right
        if operator == "gte": return left >= right
        if operator == "lt": return left < right
        if operator == "lte": return left <= right
        if operator == "eq": return abs(left - right) <= 1e-9
        return False

    def assess_rules(self, params, base_params=None):
        definitions = self.parameter_map()
        conn = self.connect()
        try:
            constraints = [dict(row) for row in conn.execute("SELECT * FROM constraint_rules WHERE enabled=1 ORDER BY display_order")]
            couplings = [dict(row) for row in conn.execute("SELECT * FROM indicator_couplings WHERE enabled=1 ORDER BY display_order")]
        finally: conn.close()
        messages = []
        from .conditional_constraint import TEMPLATE_KIND_V2, parse_template_metadata
        for rule in constraints:
            if parse_template_metadata(rule).get("template") == TEMPLATE_KIND_V2:
                continue
            left_key, right_key = rule["left_parameter"], rule.get("right_parameter")
            if left_key not in params or (right_key and right_key not in params): continue
            if is_special_value(definitions.get(left_key), params.get(left_key)) or (right_key and is_special_value(definitions.get(right_key), params.get(right_key))):
                messages.append({
                    "source": "constraint", "severity": rule.get("severity") or "warning", "title": rule["rule_name"],
                    "message": "当前特殊业务状态无法参与该数值规则。", "detail": "special_state_not_numeric",
                    "suggestion": "请确认该参数状态是否符合业务要求。", "parameters": [left_key] + ([right_key] if right_key else []),
                })
                continue
            left = float(params[left_key]); right = float(rule.get("offset") or 0)
            if right_key: right += float(rule.get("multiplier") or 1) * float(params[right_key])
            if not self._compare(left, rule["operator"], right):
                label = definitions.get(left_key, {}).get("label", left_key)
                right_label = definitions.get(right_key, {}).get("label", right_key) if right_key else "常数"
                messages.append({
                    "source": "constraint", "severity": rule.get("severity") or "warning", "title": rule["rule_name"],
                    "message": rule.get("message") or "约束规则未满足。",
                    "detail": "%s=%s，要求%s %s × %s + %s" % (label, left, rule["operator"], rule.get("multiplier"), right_label, rule.get("offset")),
                    "suggestion": "请调整%s或%s，使规则重新满足。" % (label, right_label),
                    "parameters": [left_key] + ([right_key] if right_key else []),
                })
        for item in couplings:
            a, b = item["parameter_a"], item["parameter_b"]
            if a not in params or b not in params: continue
            if is_special_value(definitions.get(a), params.get(a)) or is_special_value(definitions.get(b), params.get(b)):
                messages.append({
                    "source": "coupling", "severity": item.get("severity") or "warning", "title": item["coupling_name"],
                    "message": "当前特殊业务状态不参与常规数值耦合比较。", "detail": "special_state_not_numeric",
                    "suggestion": "请按业务状态复核该耦合关系。", "parameters": [a, b],
                })
                continue
            if item["coupling_type"] == "feasible_domain":
                rhs = float(item.get("multiplier") or 1) * float(params[a]) + float(item.get("offset") or 0)
                if not self._compare(float(params[b]), item.get("domain_operator") or "gte", rhs):
                    messages.append({
                        "source": "coupling", "severity": item.get("severity") or "warning", "title": item["coupling_name"],
                        "message": item.get("description") or "可行域耦合关系未满足。",
                        "detail": "%s当前值为%s，模型化可行边界为%s。" % (definitions.get(b, {}).get("label", b), params[b], round(rhs, 3)),
                        "suggestion": "优先将%s调整到边界%s附近，或降低%s。" % (definitions.get(b, {}).get("label", b), round(rhs, 3), definitions.get(a, {}).get("label", a)),
                        "parameters": [a, b],
                    })
            elif base_params and a in base_params and b in base_params:
                da, db = float(params[a]) - float(base_params[a]), float(params[b]) - float(base_params[b])
                violated = (item["coupling_type"] == "positive" and da * db < 0) or (item["coupling_type"] == "negative" and da * db > 0)
                one_sided = abs(da) > 1e-9 and abs(db) <= 1e-9
                if violated or one_sided:
                    relation = "同向" if item["coupling_type"] == "positive" else "反向"
                    messages.append({
                        "source": "coupling", "severity": item.get("severity") or "info", "title": item["coupling_name"],
                        "message": item.get("description") or "指标调整方向可能与耦合经验不一致。",
                        "detail": "%s变化%s，%s变化%s；经验上建议%s联动。" % (definitions.get(a, {}).get("label", a), round(da, 3), definitions.get(b, {}).get("label", b), round(db, 3), relation),
                        "suggestion": "复核%s，并结合%s同步调整。" % (definitions.get(b, {}).get("label", b), item.get("rationale") or "工程经验"),
                        "parameters": [a, b],
                    })
        order = {"error": 0, "warning": 1, "info": 2}
        messages.sort(key=lambda x: order.get(x.get("severity"), 9))
        return messages

    # --------------------- Staged product releases ---------------------
    def create_product_release(self, product_code, product_name, data):
        if self.read_only:
            raise ValueError("只读演示模式不能创建待发布成品。")
        release_id = safe_id("REL")
        stamp = now_iso()
        with self.lock:
            conn = self.connect()
            try:
                conn.execute("""INSERT INTO product_releases
                    (release_id,product_code,product_name,status,data_json,validation_json,created_at,updated_at)
                    VALUES(?,?,?,'draft',?,NULL,?,?)""", (
                    release_id, product_code, product_name,
                    json.dumps(data, ensure_ascii=False), stamp, stamp,
                ))
                self._audit(conn, "create", "product_release", release_id, {
                    "product_code": product_code, "product_name": product_name,
                })
                conn.commit()
            finally:
                conn.close()
        return self.get_product_release(release_id)

    @staticmethod
    def _release_row(row, include_data=True):
        item = dict(row)
        if include_data:
            item["data"] = json.loads(item.pop("data_json") or "{}")
        else:
            raw = json.loads(item.pop("data_json") or "{}")
            item["counts"] = dict((key, len(value)) for key, value in raw.items() if isinstance(value, list))
        item["validation"] = json.loads(item.pop("validation_json")) if item.get("validation_json") else None
        return item

    def list_product_releases(self):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM product_releases ORDER BY updated_at DESC").fetchall()
            return [self._release_row(row, include_data=False) for row in rows]
        finally:
            conn.close()

    def get_product_release(self, release_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM product_releases WHERE release_id=?", (str(release_id),)).fetchone()
            return self._release_row(row) if row else None
        finally:
            conn.close()

    def update_product_release(self, release_id, data, product_code=None, product_name=None, status=None, validation=None):
        if self.read_only:
            raise ValueError("只读演示模式不能修改待发布成品。")
        current = self.get_product_release(release_id)
        if not current:
            raise ValueError("待发布成品不存在：%s" % release_id)
        product_code = product_code if product_code is not None else current["product_code"]
        product_name = product_name if product_name is not None else current["product_name"]
        status = status or current["status"]
        validation_json = json.dumps(validation, ensure_ascii=False) if validation is not None else None
        with self.lock:
            conn = self.connect()
            try:
                conn.execute("""UPDATE product_releases
                    SET product_code=?,product_name=?,status=?,data_json=?,validation_json=?,updated_at=?
                    WHERE release_id=?""", (
                    product_code, product_name, status, json.dumps(data, ensure_ascii=False),
                    validation_json, now_iso(), str(release_id),
                ))
                self._audit(conn, "update", "product_release", str(release_id), {
                    "status": status,
                    "counts": dict((key, len(value)) for key, value in data.items() if isinstance(value, list)),
                })
                conn.commit()
            finally:
                conn.close()
        return self.get_product_release(release_id)

    def mark_product_release_active(self, release_id):
        with self.lock:
            conn = self.connect()
            try:
                stamp = now_iso()
                conn.execute("UPDATE product_releases SET status='superseded',updated_at=? WHERE status='active' AND release_id<>?", (stamp, str(release_id)))
                conn.execute("UPDATE product_releases SET status='active',activated_at=?,updated_at=? WHERE release_id=?", (stamp, stamp, str(release_id)))
                self._audit(conn, "activate", "product_release", str(release_id), {})
                conn.commit()
            finally:
                conn.close()
        return self.get_product_release(release_id)

    def delete_product_release(self, release_id):
        if self.read_only:
            raise ValueError("只读演示模式不能删除待发布成品。")
        current = self.get_product_release(release_id)
        if not current:
            raise ValueError("待发布成品不存在：%s" % release_id)
        if current.get("status") == "active":
            raise ValueError("当前已激活的发布记录不能删除。")
        with self.lock:
            conn = self.connect()
            try:
                conn.execute("DELETE FROM product_releases WHERE release_id=?", (str(release_id),))
                self._audit(conn, "delete", "product_release", str(release_id), {})
                conn.commit()
            finally:
                conn.close()
        return {"deleted": True, "release_id": str(release_id)}

    # -------------------------- Admin CRUD --------------------------
    ADMIN_TABLES = {
        "products": ("products", "product_code"),
        "parameters": ("parameter_definitions", "parameter_id"),
        "tags": ("tags", "tag_id"),
        "tag_rules": ("tag_rules", "rule_id"),
        "couplings": ("indicator_couplings", "coupling_id"),
        "constraints": ("constraint_rules", "rule_id"),
        "agreements": ("agreements", "agreement_id"),
        "models": ("model_registry", "model_kind"),
        "parameter_groups": ("parameter_groups", "group_name"),
    }

    def admin_snapshot(self):
        conn = self.connect()
        try:
            payload = {}
            for key, (table, pk) in self.ADMIN_TABLES.items():
                order = "display_order" if table in ("parameter_definitions", "indicator_couplings", "constraint_rules", "parameter_groups") else pk
                rows = [dict(row) for row in conn.execute("SELECT * FROM %s ORDER BY %s" % (table, order))]
                if key == "agreements":
                    for row in rows:
                        row["params"] = _json_object(row.pop("params_json", "{}"))
                        row["tags"] = _json_list(row.pop("tags_json", "[]"))
                payload[key] = rows
            payload["saved_schemes"] = self.list_saved()
            payload["conditional_templates"] = self.conditional_templates()
            payload["audit_log"] = [dict(row) for row in conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 100")]
            payload["database"] = {"path": str(self.db_path), "size_bytes": self.db_path.stat().st_size if self.db_path.exists() else 0, "integrity": self.integrity_check()}
            return payload
        finally: conn.close()

    def admin_upsert(self, section, item):
        if section not in self.ADMIN_TABLES or section == "models":
            raise ValueError("不支持维护该数据类型")
        table, pk = self.ADMIN_TABLES[section]
        data = dict(item or {})
        parameter_ids = set(self.parameter_map())
        tag_ids = set(self.tag_map(include_disabled=True))
        if section == "products":
            # Single-product architecture: the products CRUD page must never
            # create a second product.  Product switching goes exclusively
            # through the "待发布成品 → 备份并切换" workspace.
            code = str(data.get("product_code") or "").strip()
            conn = self.connect()
            try:
                exists = conn.execute("SELECT 1 FROM products WHERE product_code=?", (code,)).fetchone()
            finally:
                conn.close()
            if not exists:
                raise ValueError("成品信息不允许直接新增；请通过“待发布成品”工作区建立并切换业务成品。")
        if section == "tag_rules":
            if data.get("tag_id") and data.get("tag_id") not in tag_ids:
                raise ValueError("标签规则引用了不存在的标签")
            if data.get("parameter_id") and data.get("parameter_id") not in parameter_ids and data.get("parameter_id") not in ("__predicted_price_wan", "__capability_score", "__feasibility_probability"):
                raise ValueError("标签规则引用了不存在的指标")
        if section == "couplings":
            if (data.get("parameter_a") and data.get("parameter_a") not in parameter_ids) or (data.get("parameter_b") and data.get("parameter_b") not in parameter_ids):
                raise ValueError("耦合关系引用了不存在的指标")
        if section == "constraints":
            if (data.get("left_parameter") and data.get("left_parameter") not in parameter_ids) or (data.get("right_parameter") and data.get("right_parameter") not in parameter_ids):
                raise ValueError("约束规则引用了不存在的指标")
        if section == "agreements":
            params = data.pop("params", {})
            if isinstance(params, str):
                text = params.strip()
                if text:
                    try:
                        params = json.loads(text)
                    except (TypeError, ValueError):
                        raise ValueError("协议属性必须是JSON对象，或在界面上按指标逐项填写。")
                else:
                    params = {}
            if not isinstance(params, dict):
                raise ValueError("协议属性必须是JSON对象。")
            data["params_json"] = json.dumps(params, ensure_ascii=False)
            tags = data.pop("tags", [])
            if isinstance(tags, str):
                text = tags.strip()
                if text:
                    try:
                        tags = json.loads(text)
                    except (TypeError, ValueError):
                        tags = [x.strip() for x in re.split(r"[、,，;；|]+", text) if x.strip()]
                else:
                    tags = []
            if not isinstance(tags, (list, tuple)):
                tags = [] if tags in (None, "") else [tags]
            data["tags_json"] = json.dumps(list(tags), ensure_ascii=False)
            # Business-data editing never invokes the current external model.
            # Existing computed results are invalidated until an explicit user
            # calculation is requested in the recommendation system.
            data["capability_score"] = None
            data["feasibility_probability"] = None
            data["product_code"] = data.get("product_code") or self.current_product_code()
            data.setdefault("updated_at", now_iso())
        if section == "parameters":
            if isinstance(data.get("allowed_values_json"), (list, dict)):
                data["allowed_values_json"] = json.dumps(data["allowed_values_json"], ensure_ascii=False)
            if isinstance(data.get("model_value_mapping_json"), dict):
                data["model_value_mapping_json"] = json.dumps(data["model_value_mapping_json"], ensure_ascii=False)
            if isinstance(data.get("display_value_mapping_json"), dict):
                data["display_value_mapping_json"] = json.dumps(data["display_value_mapping_json"], ensure_ascii=False)
            if isinstance(data.get("special_value_keys_json"), (list, tuple)):
                data["special_value_keys_json"] = json.dumps(list(data["special_value_keys_json"]), ensure_ascii=False)
            try:
                allowed = json.loads(data.get("allowed_values_json") or "[]")
            except (TypeError, ValueError):
                raise ValueError("业务允许值必须是JSON数组，例如：[\"类型1\",\"类型2\"]")
            if not isinstance(allowed, list):
                raise ValueError("业务允许值必须是JSON数组。")
            try:
                mapping = json.loads(data.get("model_value_mapping_json") or "{}")
            except (TypeError, ValueError):
                raise ValueError("业务值到模型值的映射必须是JSON对象，例如：{\"类型1\":0}")
            if not isinstance(mapping, dict):
                raise ValueError("业务值到模型值的映射必须是JSON对象。")
            data["allowed_values_json"] = json.dumps(allowed, ensure_ascii=False) if allowed else None
            data["model_value_mapping_json"] = json.dumps(mapping, ensure_ascii=False) if mapping else None
            try:
                special_keys = json.loads(data.get("special_value_keys_json") or "[]")
            except (TypeError, ValueError):
                raise ValueError("特殊业务状态值必须是JSON数组，例如：[\"-1\"]")
            if not isinstance(special_keys, list):
                raise ValueError("特殊业务状态值必须是JSON数组。")
            display_mapping = normalize_display_mapping(
                data.get("display_value_mapping_json"), allowed + special_keys
            )
            data["display_value_mapping_json"] = json.dumps(display_mapping, ensure_ascii=False) if display_mapping else None
            value_type = str(data.get("value_type") or "number").lower()
            if value_type in ("number", "float", "integer", "ip_grade", "boolean", "bool"):
                invalid = [value for value in special_keys if normalize_numeric(value) is None]
                if invalid:
                    raise ValueError("特殊业务状态值必须能够转换为当前数值类型：%s" % invalid[0])
            data["special_value_keys_json"] = json.dumps([str(value) for value in special_keys], ensure_ascii=False) if special_keys else None

        defaults = {
            "products": {"enabled": 1},
            "parameters": {"value_type": "number", "search_type": "auto", "required": 0,
                           "auto_adjustable": 1, "decimal_places": 3, "display_order": 1,
                           "enabled": 1, "model_bound": 0},
            "tags": {"weight": 1.0, "derivation_mode": "rule", "enabled": 1},
            "tag_rules": {"operator": "gte", "rule_group": "default", "enabled": 1},
            "couplings": {"coupling_type": "positive", "domain_operator": "gte", "multiplier": 1.0,
                          "offset": 0.0, "strength": 1.0, "severity": "info", "display_order": 1,
                          "enabled": 1},
            "constraints": {"operator": "gte", "multiplier": 1.0, "offset": 0.0,
                            "severity": "warning", "display_order": 1, "enabled": 1},
            "agreements": {"agreement_source": "historical", "enabled": 1},
            "parameter_groups": {"display_order": 9999, "enabled": 1, "default_collapsed": 0},
        }.get(section, {})
        for key, value in defaults.items():
            if data.get(key) in (None, ""):
                data[key] = value

        required = {
            "products": (("product_name", "成品名称"),),
            "parameters": (("label", "指标名称"),),
            "tags": (("tag_name", "标签名称"),),
            "tag_rules": (("tag_id", "标签"), ("parameter_id", "指标")),
            "couplings": (("coupling_name", "耦合名称"), ("parameter_a", "指标A"), ("parameter_b", "指标B")),
            "constraints": (("rule_name", "约束名称"), ("left_parameter", "左侧指标")),
            "agreements": (("agreement_name", "协议名称"),),
            "parameter_groups": (("group_name", "分组名称"),),
        }.get(section, ())
        missing = [label for key, label in required if data.get(key) in (None, "")]
        if missing:
            raise ValueError("请填写必填项：%s" % "、".join(missing))
        if section == "parameters" and data.get("min_value") is not None and data.get("max_value") is not None:
            if float(data["min_value"]) > float(data["max_value"]):
                raise ValueError("指标下限不能大于上限。")
        if section == "parameter_groups":
            original = str(data.get("original_group_name") or "").strip()
            new_name = str(data.get("group_name") or "").strip()
            if original and original != new_name:
                with self.lock:
                    conn = self.connect()
                    try:
                        exists = conn.execute("SELECT 1 FROM parameter_groups WHERE group_name=?", (original,)).fetchone()
                        if not exists:
                            raise ValueError("原分组不存在：%s" % original)
                        dup = conn.execute("SELECT 1 FROM parameter_groups WHERE group_name=? AND group_name<>?", (new_name, original)).fetchone()
                        if dup:
                            raise ValueError("分组名称已存在：%s" % new_name)
                        conn.execute(
                            "UPDATE parameter_groups SET group_name=?, display_order=?, description=?, enabled=?, default_collapsed=? WHERE group_name=?",
                            (new_name, int(data.get("display_order", 9999) or 9999), data.get("description") or "",
                             int(data.get("enabled", 1) or 0), int(data.get("default_collapsed", 0) or 0), original)
                        )
                        conn.execute("UPDATE parameter_definitions SET parameter_group=? WHERE parameter_group=?", (new_name, original))
                        self._audit(conn, "upsert", section, new_name, {"renamed_from": original, "data": data})
                        conn.commit()
                    finally:
                        conn.close()
                return {"saved": True, "id": new_name}
            data.pop("original_group_name", None)
        if section == "parameters":
            group_name = str(data.get("parameter_group") or "其他").strip() or "其他"
            parameter_id = str(data.get("parameter_id") or "").strip()
            with self.lock:
                conn = self.connect()
                try:
                    conn.execute(
                        "INSERT OR IGNORE INTO parameter_groups(group_name, display_order, description, enabled, default_collapsed) VALUES(?,?,?,1,0)",
                        (group_name, 9999, "")
                    )
                    group_row = conn.execute("SELECT enabled FROM parameter_groups WHERE group_name=?", (group_name,)).fetchone()
                    if group_row and int(group_row["enabled"] or 0) == 0:
                        existing = None
                        if parameter_id:
                            existing = conn.execute(
                                "SELECT parameter_group FROM parameter_definitions WHERE parameter_id=?", (parameter_id,)
                            ).fetchone()
                        if not existing or existing["parameter_group"] != group_name:
                            raise ValueError("指标分组“%s”已停用，不能将指标移入该组。" % group_name)
                    conn.commit()
                finally:
                    conn.close()
        columns = self._table_columns(table)
        data = dict((k, v) for k, v in data.items() if k in columns)
        if not data.get(pk):
            data[pk] = safe_id({"parameters":"PAR","tags":"TAG","tag_rules":"TAGRULE","couplings":"CPL","constraints":"RULE","agreements":"AGR","products":"PRODUCT"}.get(section,"ID"))
        placeholders = ",".join("?" for _ in data)
        updates = ",".join("%s=excluded.%s" % (key, key) for key in data if key != pk)
        sql = "INSERT INTO %s(%s) VALUES(%s) ON CONFLICT(%s) DO UPDATE SET %s" % (table, ",".join(data), placeholders, pk, updates)
        with self.lock:
            conn = self.connect()
            try:
                conn.execute(sql, tuple(data.values())); self._audit(conn, "upsert", section, str(data[pk]), data); conn.commit()
            finally: conn.close()
        return {"saved": True, "id": data[pk]}

    def _table_columns(self, table):
        conn = self.connect()
        try: return self._columns(conn, table)
        finally: conn.close()

    def admin_dependencies(self, section, object_id):
        """Return references that make permanent deletion unsafe."""
        conn = self.connect()
        try:
            refs = []
            if section == "tags":
                count = conn.execute("SELECT COUNT(*) FROM tag_rules WHERE tag_id=?", (object_id,)).fetchone()[0]
                if count: refs.append({"type":"标签规则", "count":count})
                # Agreements store tags as JSON text. This is intentionally a conservative scan.
                count = conn.execute("SELECT COUNT(*) FROM agreements WHERE tags_json LIKE ?", ('%%"%s"%%' % object_id,)).fetchone()[0]
                if count: refs.append({"type":"协议标签", "count":count})
            elif section == "parameters":
                for table, column, label in [
                    ("tag_rules","parameter_id","标签规则"), ("indicator_couplings","parameter_a","耦合关系A"),
                    ("indicator_couplings","parameter_b","耦合关系B"), ("constraint_rules","left_parameter","约束左侧"),
                    ("constraint_rules","right_parameter","约束右侧")]:
                    count = conn.execute("SELECT COUNT(*) FROM %s WHERE %s=?" % (table,column), (object_id,)).fetchone()[0]
                    if count: refs.append({"type":label,"count":count})
                # Agreements store parameters as JSON text; a conservative LIKE
                # scan keeps a still-referenced field from being orphaned.
                count = conn.execute("SELECT COUNT(*) FROM agreements WHERE params_json LIKE ?", ('%%"%s"%%' % object_id,)).fetchone()[0]
                if count: refs.append({"type":"协议属性","count":count})
            elif section == "parameter_groups":
                count = conn.execute("SELECT COUNT(*) FROM parameter_definitions WHERE parameter_group=?", (object_id,)).fetchone()[0]
                if count: refs.append({"type":"指标", "count":count})
            elif section == "products":
                count = conn.execute("SELECT COUNT(*) FROM agreements WHERE product_code=?", (object_id,)).fetchone()[0]
                if count: refs.append({"type":"协议数据","count":count})
                count = conn.execute("SELECT COUNT(*) FROM model_registry WHERE product_code=?", (object_id,)).fetchone()[0]
                if count: refs.append({"type":"模型注册","count":count})
            return refs
        finally:
            conn.close()

    def admin_toggle(self, section, object_id, enabled):
        if section not in self.ADMIN_TABLES or section == "models":
            raise ValueError("该数据类型不支持启用/停用")
        table, pk = self.ADMIN_TABLES[section]
        columns = self._table_columns(table)
        if "enabled" not in columns:
            raise ValueError("该数据类型没有启用状态")
        enabled = 1 if bool(enabled) else 0
        if section == "products" and not enabled:
            raise ValueError("当前业务成品不能直接停用；请通过成品数据工作区备份并切换业务数据。")
        with self.lock:
            conn = self.connect()
            try:
                if "archived_at" in columns:
                    conn.execute("UPDATE %s SET enabled=?, archived_at=? WHERE %s=?" % (table, pk), (enabled, None if enabled else now_iso(), object_id))
                else:
                    conn.execute("UPDATE %s SET enabled=? WHERE %s=?" % (table, pk), (enabled, object_id))
                if conn.total_changes == 0:
                    raise ValueError("记录不存在")
                self._audit(conn, "enable" if enabled else "disable", section, object_id, {"enabled":enabled})
                conn.commit()
            finally:
                conn.close()
        return {"updated":True,"id":object_id,"enabled":enabled}

    def admin_delete(self, section, object_id):
        """User-facing delete means archive/disable, never physical DELETE."""
        if section not in self.ADMIN_TABLES or section in ("models", "products"):
            raise ValueError("该数据类型不允许归档")
        result = self.admin_toggle(section, object_id, False)
        result.update({"deleted":False,"archived":True})
        return result

    def admin_purge(self, section, object_id):
        """Permanently delete an already disabled record after dependency checks."""
        if section not in self.ADMIN_TABLES or section in ("models", "products"):
            raise ValueError("该数据类型不允许永久删除")
        table, pk = self.ADMIN_TABLES[section]
        dependencies = self.admin_dependencies(section, object_id)
        if dependencies:
            raise ValueError("记录仍被引用，不能永久删除：%s" % "；".join("%s%d条" % (x["type"], x["count"]) for x in dependencies))
        with self.lock:
            conn = self.connect()
            try:
                row = conn.execute("SELECT enabled FROM %s WHERE %s=?" % (table, pk), (object_id,)).fetchone()
                if row is None: raise ValueError("记录不存在")
                if int(row["enabled"] or 0): raise ValueError("请先停用/归档，再执行永久删除")
                self.create_backup("before_purge_%s" % section)
                conn.execute("DELETE FROM %s WHERE %s=?" % (table, pk), (object_id,))
                self._audit(conn, "purge", section, object_id, {"permanent":True})
                conn.commit()
            finally:
                conn.close()
        return {"purged":True,"id":object_id}

    # -------------------------- Database backup --------------------------
    def integrity_check(self, path=None):
        try:
            conn = self.connect(path)
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            tables = set(row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"))
            conn.close()
            missing = sorted(self.REQUIRED_TABLES - tables)
            blocking_missing = sorted(set(missing) - self.MIGRATABLE_TABLES)
            return {
                "ok": result == "ok" and not blocking_missing,
                "sqlite": result,
                "missing_tables": missing,
                "migratable_tables": sorted(set(missing) & self.MIGRATABLE_TABLES),
            }
        except Exception as exc:
            return {"ok": False, "sqlite": str(exc), "missing_tables": []}

    def create_backup(self, reason="manual"):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = self.backup_dir / ("protocol_demo_%s_%s.db" % (stamp, reason))
        with self.lock:
            source = self.connect(); dest = sqlite3.connect(str(target))
            try: source.backup(dest)
            finally: dest.close(); source.close()
        return {"name": target.name, "size_bytes": target.stat().st_size, "created_at": now_iso(), "integrity": self.integrity_check(target)}

    def list_backups(self):
        result = []
        for path in sorted(self.backup_dir.glob("*.db"), key=lambda p: p.stat().st_mtime, reverse=True):
            result.append({"name": path.name, "size_bytes": path.stat().st_size, "modified_at": datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")})
        return result

    def restore_backup(self, name):
        source = (self.backup_dir / Path(name).name).resolve()
        if not source.is_file() or not str(source).startswith(str(self.backup_dir.resolve())): raise ValueError("备份不存在")
        check = self.integrity_check(source)
        if not check["ok"]: raise ValueError("备份数据库校验失败: %s" % check)
        self.create_backup("before_restore")
        with self.lock:
            temp = self.db_path.with_suffix(".restore.tmp")
            shutil.copy2(str(source), str(temp))
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                if sidecar.exists(): sidecar.unlink()
            os.replace(str(temp), str(self.db_path))
        self._initialize()
        return {"restored": True, "name": source.name}

    def restore_uploaded(self, uploaded_path):
        path = Path(uploaded_path)
        check = self.integrity_check(path)
        if not check["ok"]: raise ValueError("上传数据库校验失败: %s" % check)
        backup = self.create_backup("before_upload_restore")
        with self.lock:
            temp = self.db_path.with_suffix(".upload.tmp")
            shutil.copy2(str(path), str(temp))
            for suffix in ("-wal", "-shm"):
                sidecar = Path(str(self.db_path) + suffix)
                if sidecar.exists(): sidecar.unlink()
            os.replace(str(temp), str(self.db_path))
        self._initialize()
        return {"restored": True, "previous_backup": backup["name"]}

    def export_json(self):
        return self.admin_snapshot()
