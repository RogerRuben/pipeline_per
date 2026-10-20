# -*- coding: utf-8 -*-
"""Staged product master-data releases.

Draft releases deliberately separate file parsing from cross-module and model
validation. Operators can therefore prepare a new product while the currently
active product keeps running, then validate and activate it in one explicit
operation.
"""
from __future__ import print_function

import csv
import hashlib
import io
import json
import re
from datetime import datetime

from .data_master import (
    _json_allowed,
    clean,
    integer,
    normalize_coupling_type,
    normalize_derivation,
    normalize_operator,
    normalize_preference,
    normalize_search_type,
    normalize_severity,
    normalize_value_type,
    num,
)
from .model_field_types import model_types_compatible
from .store import safe_id
from .historical_onboarding import HistoricalProductOnboarding
from .wide_import import WideTableParser, parse_bool, read_table_bytes
from .xlsx_utils import read_workbook_bytes


SECTIONS = ("products", "parameters", "tags", "tag_rules", "couplings", "constraints", "agreements")

SECTION_TITLES = {
    "products": "成品信息",
    "parameters": "指标定义",
    "tags": "标签字典",
    "tag_rules": "标签规则",
    "couplings": "耦合关系",
    "constraints": "约束规则",
    "agreements": "历史协议",
}

HEADER_ALIASES = {
    "products": {
        "product_code": ("product_code", "成品代号", "产品代号"),
        "product_name": ("product_name", "成品名称", "产品名称"),
        "product_description": ("product_description", "成品说明", "产品说明"),
        "enabled": ("enabled", "是否启用"),
    },
    "parameters": {
        "parameter_id": ("parameter_id", "指标编号", "字段编号"),
        "label": ("label", "指标名称", "字段名称"),
        "unit": ("unit", "单位"),
        "value_type": ("value_type", "取值类型", "数据类型"),
        "search_type": ("search_type", "搜索类型"),
        "min_value": ("min_value", "工程下限", "最小值"),
        "max_value": ("max_value", "工程上限", "最大值"),
        "preference": ("preference", "效能方向", "偏好方向"),
        "description": ("description", "指标说明", "字段说明"),
        "adjustment_hint": ("adjustment_hint", "调整提示"),
        "allowed_values": ("allowed_values", "allowed_values_json", "允许值"),
        "model_value_mapping_json": ("model_value_mapping_json", "模型取值映射", "模型取值映射(JSON)"),
        "required": ("required", "是否必填"),
        "auto_adjustable": ("auto_adjustable", "允许自动调整"),
        "decimal_places": ("decimal_places", "显示小数位"),
        "display_order": ("display_order", "显示顺序"),
        "enabled": ("enabled", "是否启用"),
        "model_bound": ("model_bound", "模型字段", "是否模型字段"),
    },
    "tags": {
        "tag_id": ("tag_id", "标签编号"),
        "tag_name": ("tag_name", "标签名称"),
        "tag_group": ("tag_group", "标签分组"),
        "weight": ("weight", "匹配权重"),
        "derivation_mode": ("derivation_mode", "生成判定方式", "标签判定方式"),
        "description": ("description", "标签说明"),
        "enabled": ("enabled", "是否启用"),
    },
    "tag_rules": {
        "rule_id": ("rule_id", "规则编号"),
        "tag_id": ("tag_id", "标签编号"),
        "parameter_id": ("parameter_id", "指标编号", "字段编号"),
        "operator": ("operator", "比较关系"),
        "value1": ("value1", "条件值1"),
        "value2": ("value2", "条件值2"),
        "rule_group": ("rule_group", "规则组"),
        "enabled": ("enabled", "是否启用"),
    },
    "couplings": {
        "coupling_id": ("coupling_id", "关系编号", "耦合编号"),
        "coupling_name": ("coupling_name", "关系名称", "耦合名称"),
        "coupling_type": ("coupling_type", "关系类型", "耦合类型"),
        "parameter_a": ("parameter_a", "指标A"),
        "parameter_b": ("parameter_b", "指标B"),
        "domain_operator": ("domain_operator", "可行域比较"),
        "multiplier": ("multiplier", "系数"),
        "offset": ("offset", "偏置"),
        "strength": ("strength", "作用强度"),
        "severity": ("severity", "提示级别"),
        "description": ("description", "关系说明"),
        "rationale": ("rationale", "设置依据"),
        "display_order": ("display_order", "显示顺序"),
        "enabled": ("enabled", "是否启用"),
    },
    "constraints": {
        "rule_id": ("rule_id", "规则编号"),
        "rule_name": ("rule_name", "规则名称"),
        "left_parameter": ("left_parameter", "左侧指标"),
        "operator": ("operator", "比较关系"),
        "right_parameter": ("right_parameter", "右侧指标"),
        "multiplier": ("multiplier", "系数"),
        "offset": ("offset", "偏置"),
        "severity": ("severity", "提示级别"),
        "message": ("message", "违反提示"),
        "rationale": ("rationale", "设置依据"),
        "display_order": ("display_order", "显示顺序"),
        "enabled": ("enabled", "是否启用"),
    },
}

REQUIRED_COLUMNS = {
    "products": ("product_code", "product_name"),
    "parameters": ("parameter_id", "label", "value_type"),
    "tags": ("tag_id", "tag_name"),
    "tag_rules": ("rule_id", "tag_id", "parameter_id", "operator"),
    "couplings": ("coupling_id", "coupling_name", "coupling_type", "parameter_a", "parameter_b"),
    "constraints": ("rule_id", "rule_name", "left_parameter", "operator"),
}

PRIMARY_KEYS = {
    "products": "product_code",
    "parameters": "parameter_id",
    "tags": "tag_id",
    "tag_rules": "rule_id",
    "couplings": "coupling_id",
    "constraints": "rule_id",
    "agreements": "agreement_id",
}

CSV_COLUMNS = {
    "products": (
        ("product_code", "成品代号"), ("product_name", "成品名称"),
        ("product_description", "成品说明"), ("enabled", "是否启用"),
    ),
    "parameters": (
        ("parameter_id", "指标编号"), ("label", "指标名称"), ("unit", "单位"),
        ("value_type", "取值类型"), ("search_type", "搜索类型"),
        ("min_value", "工程下限"), ("max_value", "工程上限"),
        ("preference", "效能方向"), ("description", "指标说明"),
        ("adjustment_hint", "调整提示"), ("allowed_values_json", "允许值"),
        ("model_value_mapping_json", "模型取值映射(JSON)"),
        ("required", "是否必填"), ("auto_adjustable", "允许自动调整"),
        ("decimal_places", "显示小数位"), ("display_order", "显示顺序"),
        ("enabled", "是否启用"), ("model_bound", "是否模型字段"),
    ),
    "tags": (
        ("tag_id", "标签编号"), ("tag_name", "标签名称"), ("tag_group", "标签分组"),
        ("weight", "匹配权重"), ("derivation_mode", "生成判定方式"),
        ("description", "标签说明"), ("enabled", "是否启用"),
    ),
    "tag_rules": (
        ("rule_id", "规则编号"), ("tag_id", "标签编号"), ("parameter_id", "指标编号"),
        ("operator", "比较关系"), ("value1", "条件值1"), ("value2", "条件值2"),
        ("rule_group", "规则组"), ("enabled", "是否启用"),
    ),
    "couplings": (
        ("coupling_id", "关系编号"), ("coupling_name", "关系名称"),
        ("coupling_type", "关系类型"), ("parameter_a", "指标A"), ("parameter_b", "指标B"),
        ("domain_operator", "可行域比较"), ("multiplier", "系数"), ("offset", "偏置"),
        ("strength", "作用强度"), ("severity", "提示级别"), ("description", "关系说明"),
        ("rationale", "设置依据"), ("display_order", "显示顺序"), ("enabled", "是否启用"),
    ),
    "constraints": (
        ("rule_id", "规则编号"), ("rule_name", "规则名称"), ("left_parameter", "左侧指标"),
        ("operator", "比较关系"), ("right_parameter", "右侧指标"), ("multiplier", "系数"),
        ("offset", "偏置"), ("severity", "提示级别"), ("message", "违反提示"),
        ("rationale", "设置依据"), ("display_order", "显示顺序"), ("enabled", "是否启用"),
    ),
}

PACKAGE_FORMAT = "industrial-product-release-1.0"


def _header_key(value):
    return re.sub(r"\s+", "", clean(value)).lower()


def _bool(value, default=1):
    return default if clean(value) == "" else parse_bool(value)


class ProductReleaseService(object):
    def __init__(self, store, runtime):
        self.store = store
        self.runtime = runtime
        self.historical_onboarding = HistoricalProductOnboarding()

    def analyze_history(self, filename, raw, product_code, product_name, missing_tokens=None):
        return self.historical_onboarding.analyze(
            filename, raw, product_code, product_name, missing_tokens=missing_tokens,
        )

    def create_from_history(self, filename, raw, product_code, product_name, missing_tokens=None):
        report = self.analyze_history(filename, raw, product_code, product_name, missing_tokens)
        release = self.store.create_product_release(
            report["product_code"], report["product_name"], report["data"],
        )
        result = dict((key, value) for key, value in report.items() if key != "data")
        result["release"] = release
        return result

    @staticmethod
    def _sheet_csv(rows):
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        for row in rows:
            writer.writerow(row)
        return ("\ufeff" + out.getvalue()).encode("utf-8")

    def import_maintenance_workbook(self, release_id, filename, raw, skip_invalid=False):
        original = self.get(release_id)
        try:
            return self._import_maintenance_workbook(release_id, filename, raw, skip_invalid)
        except Exception:
            self.store.update_product_release(
                release_id, original["data"], product_code=original.get("product_code"),
                product_name=original.get("product_name"), status=original.get("status") or "draft",
                validation=original.get("validation"),
            )
            raise

    def _import_maintenance_workbook(self, release_id, filename, raw, skip_invalid=False):
        if not clean(filename).lower().endswith(".xlsx"):
            raise ValueError("维护工作簿仅支持.xlsx文件。")
        workbook = read_workbook_bytes(raw)
        sheet_sections = (
            ("成品信息", "products"), ("指标定义", "parameters"), ("标签字典", "tags"),
            ("标签规则", "tag_rules"), ("耦合关系", "couplings"),
            ("约束规则", "constraints"), ("历史协议", "agreements"),
        )
        if not any(sheet in workbook for sheet, _section in sheet_sections):
            raise ValueError("工作簿中没有可识别的DataMaster数据工作表。")
        reports = []
        for sheet, section in sheet_sections:
            if sheet not in workbook:
                reports.append({"sheet": sheet, "section": section, "status": "missing", "message": "工作表不存在，保留草稿原数据。"})
                continue
            rows = [row for row in workbook[sheet] if any(clean(value) for value in row)]
            if len(rows) <= 1:
                self.set_section(release_id, section, [])
                reports.append({"sheet": sheet, "section": section, "status": "cleared", "valid_count": 0, "invalid_count": 0})
                continue
            report = self.parse_module(release_id, section, "%s.csv" % section, self._sheet_csv(rows))
            if report.get("invalid_count") and not skip_invalid:
                first = next((row for row in report.get("rows", []) if not row.get("valid")), None)
                detail = "；".join(first.get("errors") or []) if first else "存在格式错误"
                raise ValueError("%s有%d条无效记录：%s" % (sheet, report["invalid_count"], detail))
            items = [row["item"] for row in report.get("rows", []) if row.get("valid")]
            if items or not report.get("row_count"):
                self.set_section(release_id, section, items)
                status = "imported"
            else:
                status = "skipped"
            reports.append({
                "sheet": sheet, "section": section, "status": status,
                "valid_count": report.get("valid_count", 0), "invalid_count": report.get("invalid_count", 0),
                "warnings": report.get("global_warnings") or [],
            })
        return {"release": self.get(release_id), "modules": reports}

    def _parameter_skeleton(self):
        rows = []
        for index, spec in enumerate(self.runtime.all_feature_specs(), 1):
            value_type = "ip_grade" if spec.get("parser") == "ip_grade" else spec.get("dtype") or spec.get("type") or "number"
            if value_type in ("integer", "numeric"):
                value_type = "number"
            allowed = spec.get("allowed_values")
            rows.append({
                "parameter_id": spec["key"],
                "label": spec.get("label") or spec["key"],
                "unit": spec.get("unit") or "",
                "value_type": value_type,
                "min_value": spec.get("min"),
                "max_value": spec.get("max"),
                "preference": spec.get("preference") or "neutral",
                "description": spec.get("description") or "",
                "adjustment_hint": spec.get("adjustment_hint") or "",
                "allowed_values_json": json.dumps(allowed, ensure_ascii=False) if allowed else None,
                "model_value_mapping_json": None,
                "search_type": spec.get("search_type") or "auto",
                "required": 1 if spec.get("required", True) else 0,
                "auto_adjustable": 1 if spec.get("auto_adjustable", True) else 0,
                "decimal_places": int(spec.get("decimal_places", 3)),
                "display_order": index,
                "enabled": 1,
                "model_bound": 1,
            })
        return rows

    def create(self, product_code=None, product_name=None, product_description="", seed_schema=False):
        requested_code = clean(product_code)
        if requested_code:
            code = requested_code
        elif seed_schema:
            # Explicit developer/test compatibility path: a caller asking for
            # a model skeleton also gets that model's identity by default.
            code = clean(self.runtime.schema.get("product_code"))
        else:
            code = clean(self.store.current_product_code()) or "NEW_PRODUCT"
        model_code = clean(self.runtime.schema.get("product_code"))
        default_name = clean(self.runtime.schema.get("product_name")) if code == model_code else code
        name = clean(product_name) or default_name or code
        if not code:
            raise ValueError("成品代号不能为空。")
        data = dict((key, []) for key in SECTIONS)
        data["products"] = [{
            "product_code": code, "product_name": name,
            "product_description": clean(product_description), "enabled": 1,
        }]
        if seed_schema:
            data["parameters"] = self._parameter_skeleton()
        return self.store.create_product_release(code, name, data)

    def clone_current(self):
        snapshot = self.store.admin_snapshot()
        products = snapshot.get("products") or []
        if len(products) != 1:
            raise ValueError("当前运行数据库必须且只能包含一个成品，才能复制为草稿。")
        product = products[0]
        data = {
            "products": [dict(product)],
            "parameters": [dict(item) for item in snapshot.get("parameters", [])],
            "tags": [dict(item) for item in snapshot.get("tags", [])],
            "tag_rules": [dict(item) for item in snapshot.get("tag_rules", [])],
            "couplings": [dict(item) for item in snapshot.get("couplings", [])],
            "constraints": [dict(item) for item in snapshot.get("constraints", [])],
            "agreements": [dict(item) for item in snapshot.get("agreements", [])],
        }
        return self.store.create_product_release(
            clean(product.get("product_code")), clean(product.get("product_name")), data
        )

    @staticmethod
    def _canonical_json(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)

    def export_package(self, release_id):
        release = self.get(release_id)
        core = {
            "format": PACKAGE_FORMAT,
            "product_code": release["product_code"],
            "product_name": release["product_name"],
            "data": release["data"],
        }
        package = dict(core)
        package.update({
            "exported_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "source_release_id": release["release_id"],
            "source_status": release["status"],
            "payload_sha256": hashlib.sha256(self._canonical_json(core).encode("utf-8")).hexdigest(),
        })
        return (json.dumps(package, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    def import_package(self, raw):
        try:
            package = json.loads(raw.decode("utf-8-sig"))
        except Exception as exc:
            raise ValueError("离线发布包不是有效UTF-8 JSON：%s" % exc)
        if package.get("format") != PACKAGE_FORMAT:
            raise ValueError("不支持的离线发布包格式：%s" % package.get("format"))
        data = package.get("data")
        if not isinstance(data, dict):
            raise ValueError("离线发布包缺少data对象。")
        missing = [section for section in SECTIONS if not isinstance(data.get(section), list)]
        if missing:
            raise ValueError("离线发布包缺少数据模块：%s" % "、".join(missing))
        core = {
            "format": PACKAGE_FORMAT,
            "product_code": clean(package.get("product_code")),
            "product_name": clean(package.get("product_name")),
            "data": data,
        }
        expected = hashlib.sha256(self._canonical_json(core).encode("utf-8")).hexdigest()
        if clean(package.get("payload_sha256")) != expected:
            raise ValueError("离线发布包完整性校验失败，文件可能不完整或已被修改。")
        products = data.get("products") or []
        if len(products) != 1:
            raise ValueError("离线发布包中的成品信息必须且只能有一条。")
        product_code = clean(products[0].get("product_code"))
        product_name = clean(products[0].get("product_name"))
        if product_code != core["product_code"] or product_name != core["product_name"]:
            raise ValueError("离线发布包头部与成品信息不一致。")
        # Older delivery packages used ``integer``/``numeric`` as a storage
        # value type.  Business data stores all numeric values as ``number``;
        # integer stepping belongs to search_type.  Canonicalize only after
        # the package hash has been verified so transport integrity remains
        # meaningful while old packages stay importable.
        for parameter in data.get("parameters", []):
            if clean(parameter.get("value_type")).lower() in ("integer", "numeric"):
                parameter["value_type"] = "number"
        return self.store.create_product_release(product_code, product_name, data)

    def module_template(self, release_id, section):
        release = self.get(release_id)
        if section == "agreements":
            parameters = release["data"].get("parameters", [])
            tags = release["data"].get("tags", [])
            return WideTableParser(parameters, tags, release["product_code"]).template_csv()
        columns = CSV_COLUMNS.get(section)
        if not columns:
            raise ValueError("该模块不支持CSV模板：%s" % section)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow([label for _key, label in columns])
        current = release["data"].get(section, [])
        if current:
            # The first existing row is a useful, editable example. Full data is
            # transported by the release package, so templates stay lightweight.
            item = current[0]
            writer.writerow([
                json.dumps(item.get(key), ensure_ascii=False) if isinstance(item.get(key), (list, dict)) else item.get(key, "")
                for key, _label in columns
            ])
        return ("\ufeff" + out.getvalue()).encode("utf-8")

    def list(self):
        return self.store.list_product_releases()

    def get(self, release_id):
        item = self.store.get_product_release(release_id)
        if not item:
            raise ValueError("待发布成品不存在：%s" % release_id)
        return item

    def delete(self, release_id):
        return self.store.delete_product_release(release_id)

    def set_section(self, release_id, section, items):
        if section not in SECTIONS:
            raise ValueError("不支持的成品数据模块：%s" % section)
        if not isinstance(items, list):
            raise ValueError("模块数据必须是JSON数组。")
        release = self.get(release_id)
        data = release["data"]
        prefixes = {"parameters":"PAR", "tags":"TAG", "tag_rules":"TAGRULE",
                    "couplings":"CPL", "constraints":"RULE", "agreements":"AGR"}
        section_defaults = {
            "parameters": {"value_type":"number", "search_type":"auto", "required":0,
                           "auto_adjustable":1, "decimal_places":3, "display_order":1,
                           "enabled":1, "model_bound":0},
            "tags": {"weight":1.0, "derivation_mode":"rule", "enabled":1},
            "tag_rules": {"operator":"gte", "rule_group":"default", "enabled":1},
            "couplings": {"coupling_type":"positive", "domain_operator":"gte", "multiplier":1.0,
                          "offset":0.0, "strength":1.0, "severity":"info", "display_order":1, "enabled":1},
            "constraints": {"operator":"gte", "multiplier":1.0, "offset":0.0,
                            "severity":"warning", "display_order":1, "enabled":1},
        }.get(section, {})
        primary_key = PRIMARY_KEYS.get(section)
        prepared = []
        for raw in items:
            item = dict(raw or {})
            if section == "products":
                item["product_code"] = clean(item.get("product_code")) or release["product_code"]
                item["product_name"] = clean(item.get("product_name")) or release["product_name"]
                item.setdefault("enabled", 1)
            elif primary_key and not clean(item.get(primary_key)):
                item[primary_key] = safe_id(prefixes.get(section, "ID"))
            for key, value in section_defaults.items():
                if item.get(key) in (None, ""):
                    item[key] = value
            if section == "agreements":
                item["product_code"] = clean(item.get("product_code")) or release["product_code"]
                item.setdefault("agreement_source", "historical")
                item.setdefault("params", {})
                item.setdefault("tags", [])
                item.setdefault("enabled", 1)
            prepared.append(item)
        data[section] = prepared
        if section == "products" and prepared:
            release["product_code"] = clean(prepared[0].get("product_code"))
            release["product_name"] = clean(prepared[0].get("product_name"))
        return self.store.update_product_release(
            release_id, data, product_code=release.get("product_code"),
            product_name=release.get("product_name"), status="draft",
            validation=None,
        )

    def parse_module(self, release_id, section, filename, raw):
        release = self.get(release_id)
        if section == "agreements":
            parameters = [dict(item, model_bound=0) for item in release["data"].get("parameters", [])]
            tags = release["data"].get("tags", [])
            report = WideTableParser(parameters, tags, release["product_code"]).parse(filename, raw)
            report["section"] = section
            return report
        if section not in HEADER_ALIASES:
            raise ValueError("不支持导入该模块：%s" % section)
        table = read_table_bytes(filename, raw)
        table = [row for row in table if any(clean(value) for value in row)]
        if not table:
            raise ValueError("%s文件为空。" % SECTION_TITLES[section])
        alias_map = {}
        for canonical, aliases in HEADER_ALIASES[section].items():
            for alias in aliases:
                alias_map[_header_key(alias)] = canonical
        mapping = {}
        unknown = []
        for index, header in enumerate(table[0]):
            canonical = alias_map.get(_header_key(header))
            if canonical:
                mapping[index] = canonical
            elif clean(header):
                unknown.append(clean(header))
        missing = [name for name in REQUIRED_COLUMNS[section] if name not in mapping.values()]
        if missing:
            raise ValueError("%s缺少必需列：%s" % (SECTION_TITLES[section], "、".join(missing)))
        rows = []
        seen = set()
        for row_number, values in enumerate(table[1:], 2):
            raw_item = dict((name, values[index] if index < len(values) else "") for index, name in mapping.items())
            errors = []
            try:
                item = self._normalize_item(section, raw_item, row_number)
            except Exception as exc:
                item = raw_item
                errors.append(str(exc))
            primary = clean(item.get(PRIMARY_KEYS[section]))
            if not primary:
                errors.append("%s不能为空" % PRIMARY_KEYS[section])
            elif primary in seen:
                errors.append("%s在文件内重复：%s" % (PRIMARY_KEYS[section], primary))
            seen.add(primary)
            rows.append({"row_number": row_number, "item": item, "errors": errors, "warnings": [], "valid": not errors})
        return {
            "section": section,
            "filename": filename,
            "row_count": len(rows),
            "valid_count": sum(1 for row in rows if row["valid"]),
            "invalid_count": sum(1 for row in rows if not row["valid"]),
            "global_warnings": (["未识别列已忽略：%s" % "、".join(unknown)] if unknown else []),
            "rows": rows,
        }

    def stage_module(self, release_id, section, report, skip_invalid=False):
        if report.get("invalid_count") and not skip_invalid:
            raise ValueError("文件中存在%d条格式错误记录；请修正或选择跳过无效行。" % report["invalid_count"])
        items = [row["item"] for row in report.get("rows", []) if row.get("valid")]
        if not items:
            raise ValueError("没有可暂存的有效记录。")
        updated = self.set_section(release_id, section, items)
        return {"staged": True, "section": section, "count": len(items), "release": updated}

    def _normalize_item(self, section, row, row_number):
        if section == "products":
            return {
                "product_code": clean(row.get("product_code")),
                "product_name": clean(row.get("product_name")),
                "product_description": clean(row.get("product_description")),
                "enabled": _bool(row.get("enabled")),
            }
        if section == "parameters":
            allowed = _json_allowed(row.get("allowed_values"))
            mapping = clean(row.get("model_value_mapping_json"))
            if mapping:
                parsed_mapping = json.loads(mapping)
                if not isinstance(parsed_mapping, dict):
                    raise ValueError("模型取值映射必须是JSON对象")
                mapping = json.dumps(parsed_mapping, ensure_ascii=False)
            return {
                "parameter_id": clean(row.get("parameter_id")), "label": clean(row.get("label")),
                "unit": clean(row.get("unit")), "value_type": normalize_value_type(row.get("value_type")),
                "min_value": num(row.get("min_value")), "max_value": num(row.get("max_value")),
                "preference": normalize_preference(row.get("preference")),
                "description": clean(row.get("description")), "adjustment_hint": clean(row.get("adjustment_hint")),
                "allowed_values_json": json.dumps(allowed, ensure_ascii=False) if allowed else None,
                "model_value_mapping_json": mapping or None,
                "search_type": normalize_search_type(row.get("search_type")),
                "required": _bool(row.get("required")), "auto_adjustable": _bool(row.get("auto_adjustable")),
                "decimal_places": integer(row.get("decimal_places"), 3),
                "display_order": integer(row.get("display_order"), row_number - 1),
                "enabled": _bool(row.get("enabled")), "model_bound": _bool(row.get("model_bound")),
            }
        if section == "tags":
            return {
                "tag_id": clean(row.get("tag_id")), "tag_name": clean(row.get("tag_name")),
                "tag_group": clean(row.get("tag_group")), "weight": num(row.get("weight")) if clean(row.get("weight")) else 1.0,
                "derivation_mode": normalize_derivation(row.get("derivation_mode")),
                "description": clean(row.get("description")), "enabled": _bool(row.get("enabled")),
            }
        if section == "tag_rules":
            return {
                "rule_id": clean(row.get("rule_id")), "tag_id": clean(row.get("tag_id")),
                "parameter_id": clean(row.get("parameter_id")), "operator": normalize_operator(row.get("operator")),
                "value1": clean(row.get("value1")), "value2": clean(row.get("value2")),
                "rule_group": clean(row.get("rule_group")) or "default", "enabled": _bool(row.get("enabled")),
            }
        if section == "couplings":
            return {
                "coupling_id": clean(row.get("coupling_id")), "coupling_name": clean(row.get("coupling_name")),
                "coupling_type": normalize_coupling_type(row.get("coupling_type")),
                "parameter_a": clean(row.get("parameter_a")), "parameter_b": clean(row.get("parameter_b")),
                "domain_operator": normalize_operator(row.get("domain_operator")),
                "multiplier": num(row.get("multiplier")), "offset": num(row.get("offset")),
                "strength": num(row.get("strength")), "severity": normalize_severity(row.get("severity")),
                "description": clean(row.get("description")), "rationale": clean(row.get("rationale")),
                "display_order": integer(row.get("display_order"), row_number - 1), "enabled": _bool(row.get("enabled")),
            }
        if section == "constraints":
            return {
                "rule_id": clean(row.get("rule_id")), "rule_name": clean(row.get("rule_name")),
                "left_parameter": clean(row.get("left_parameter")), "operator": normalize_operator(row.get("operator")),
                "right_parameter": clean(row.get("right_parameter")) or None,
                "multiplier": num(row.get("multiplier")) if clean(row.get("multiplier")) else 1.0,
                "offset": num(row.get("offset")) if clean(row.get("offset")) else 0.0,
                "severity": normalize_severity(row.get("severity")), "message": clean(row.get("message")),
                "rationale": clean(row.get("rationale")),
                "display_order": integer(row.get("display_order"), row_number - 1), "enabled": _bool(row.get("enabled")),
            }
        raise ValueError("不支持的数据模块：%s" % section)

    @staticmethod
    def _duplicates(items, key):
        seen = set()
        duplicate = set()
        for item in items:
            value = clean(item.get(key))
            if value in seen:
                duplicate.add(value)
            seen.add(value)
        return sorted(value for value in duplicate if value)

    def validate(self, release_id):
        """Check only business-data structure; HTTP model contracts are external.

        A draft commonly describes the *next* product while the currently
        running price/effectiveness services still describe the old product.
        Comparing those identities here would make the staging workspace
        unusable. Runtime readiness is diagnosed separately by Application.
        """
        release = self.get(release_id)
        data = release["data"]
        errors = []
        warnings = []
        counts = dict((section, len(data.get(section, []))) for section in SECTIONS)
        products = data.get("products", [])
        if len(products) != 1:
            errors.append("成品信息必须且只能有一条。")
        product_code = clean(products[0].get("product_code")) if products else clean(release.get("product_code"))
        for section, key in PRIMARY_KEYS.items():
            required_fields = REQUIRED_COLUMNS.get(section, ("agreement_id", "agreement_name"))
            for index, item in enumerate(data.get(section, []), 1):
                absent_fields = [field for field in required_fields if clean(item.get(field)) == ""]
                if absent_fields:
                    errors.append("%s第%d条缺少必填字段：%s" % (
                        SECTION_TITLES[section], index, "、".join(absent_fields)
                    ))
            duplicate = self._duplicates(data.get(section, []), key)
            if duplicate:
                errors.append("%s存在重复编号：%s" % (SECTION_TITLES[section], "、".join(duplicate)))

        parameters = dict((clean(item.get("parameter_id")), item) for item in data.get("parameters", []) if clean(item.get("parameter_id")))
        tags = set(clean(item.get("tag_id")) for item in data.get("tags", []) if clean(item.get("tag_id")))
        if not parameters:
            errors.append("至少需要一条指标定义。")
        supported_types = set(("number", "boolean", "ip_grade", "enum", "text"))
        supported_search = set(("auto", "continuous", "integer", "ordered_discrete", "unordered_enum", "boolean"))
        for key, item in parameters.items():
            if item.get("value_type") not in supported_types:
                errors.append("指标%s的取值类型无效：%s。" % (key, item.get("value_type")))
            if item.get("search_type") not in supported_search:
                errors.append("指标%s的搜索类型无效：%s。" % (key, item.get("search_type")))
            if item.get("min_value") is not None and item.get("max_value") is not None:
                try:
                    if float(item["min_value"]) > float(item["max_value"]):
                        errors.append("指标%s的工程下限不能高于工程上限。" % key)
                except (TypeError, ValueError):
                    errors.append("指标%s的工程上下限必须是数字。" % key)
        output_fields = set(("__predicted_price_wan", "__capability_score", "__feasibility_probability"))
        for item in data.get("tag_rules", []):
            if item.get("tag_id") not in tags:
                errors.append("标签规则%s引用不存在的标签%s。" % (item.get("rule_id"), item.get("tag_id")))
            if item.get("parameter_id") not in parameters and item.get("parameter_id") not in output_fields:
                errors.append("标签规则%s引用不存在的指标%s。" % (item.get("rule_id"), item.get("parameter_id")))
        for item in data.get("couplings", []):
            if item.get("parameter_a") not in parameters or item.get("parameter_b") not in parameters:
                errors.append("耦合关系%s引用不存在的指标%s/%s。" % (item.get("coupling_id"), item.get("parameter_a"), item.get("parameter_b")))
        for item in data.get("constraints", []):
            if item.get("left_parameter") not in parameters or (item.get("right_parameter") and item.get("right_parameter") not in parameters):
                errors.append("约束规则%s引用不存在的指标%s/%s。" % (item.get("rule_id"), item.get("left_parameter"), item.get("right_parameter") or "常数"))
        required = set(key for key, parameter in parameters.items() if parameter.get("required"))
        for item in data.get("agreements", []):
            if clean(item.get("product_code")) not in ("", product_code):
                errors.append("协议%s的成品代号与草稿不一致。" % item.get("agreement_id"))
            absent = sorted(key for key in required if (item.get("params") or {}).get(key) in (None, ""))
            if absent:
                warnings.append("协议%s暂缺业务必填属性：%s；可继续维护，是否能计算由目标HTTP模型服务决定。" % (item.get("agreement_id"), "、".join(absent)))
            unknown_tags = sorted(set(item.get("tags") or []) - tags)
            if unknown_tags:
                errors.append("协议%s引用不存在的标签：%s" % (item.get("agreement_id"), "、".join(unknown_tags)))
        warnings.insert(0, "本检查只验证业务数据结构和本地引用，不读取、不比较当前价格或效能HTTP服务Schema。")
        report = {
            "valid": not errors, "errors": errors, "warnings": warnings, "counts": counts,
            "scope": "business_data_only", "model_contract_checked": False,
        }
        self.store.update_product_release(release_id, data, status="validated" if report["valid"] else "draft", validation=report)
        return report

    def activate(self, release_id):
        report = self.validate(release_id)
        if not report["valid"]:
            raise ValueError("草稿存在无法写入数据库的结构问题，不能切换业务数据。")
        release = self.get(release_id)
        result = self.store.replace_from_datamaster(
            release["data"], evaluate_agreements=False, sync_model_contract=False,
        )
        self.store.mark_product_release_active(release_id)
        return {
            "activated": True, "release_id": release_id, "validation": report,
            "commit_result": result, "model_services_called": False,
        }
