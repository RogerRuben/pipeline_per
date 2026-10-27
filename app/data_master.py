# -*- coding: utf-8 -*-
"""DataMaster import/export for non-programmer product maintenance."""
from __future__ import print_function

import json
import re
from collections import OrderedDict

from .wide_import import parse_bool, split_tags
from .xlsx_utils import read_workbook_bytes, write_workbook_bytes
from .display_mapping import dump_display_mapping, normalize_display_mapping


SHEETS = OrderedDict([
    ("成品信息", ["成品代号", "成品名称", "成品说明", "是否启用"]),
    ("指标定义", ["指标编号", "指标名称", "指标分组", "单位", "取值类型", "搜索类型", "工程下限", "工程上限", "效能方向", "指标说明", "调整提示", "允许值", "是否必填", "允许自动调整", "显示小数位", "显示顺序", "是否启用", "模型取值映射(JSON)", "前端显示映射(JSON)", "特殊业务状态值(JSON数组)", "历史观测下限", "历史观测上限"]),
    ("ValueMappings", ["parameter_id", "business_value", "display_value", "model_value", "allowed", "special"]),
    ("指标分组", ["分组名称", "显示顺序", "说明", "是否启用", "默认折叠"]),
    ("标签字典", ["标签编号", "标签名称", "标签分组", "匹配权重", "生成判定方式", "标签说明", "是否启用"]),
    ("标签规则", ["规则编号", "标签编号", "指标编号", "比较关系", "条件值1", "条件值2", "规则组", "是否启用"]),
    ("耦合关系", ["关系编号", "关系名称", "关系类型", "指标A", "指标B", "可行域比较", "系数", "偏置", "作用强度", "提示级别", "关系说明", "设置依据", "显示顺序", "是否启用"]),
    ("约束规则", ["规则编号", "规则名称", "左侧指标", "比较关系", "右侧指标", "系数", "偏置", "提示级别", "违反提示", "设置依据", "显示顺序", "是否启用", "规则类型", "约束组", "模板元数据(JSON)"]),
    ("历史协议", ["协议编号", "协议名称", "方案定位", "协议来源", "来源年份", "供应方类型", "历史价格(万元)", "标签"]),
    ("模型字段绑定", ["模型类型", "字段编号", "字段名称", "字段来源", "数据类型", "单位", "是否必填", "缺失策略", "数据库配置值", "训练均值", "模型版本", "是否启用"]),
])

# Sheets that older workbooks may omit; they are derived from existing data.
OPTIONAL_SHEETS = {"指标分组", "ValueMappings"}


def clean(value):
    return "" if value is None else str(value).strip()


def num(value, allow_none=True):
    if clean(value) == "" and allow_none:
        return None
    return float(value)


def integer(value, default=0):
    if clean(value) == "":
        return int(default)
    return int(float(value))


def _json_scalar(value):
    """Decode JSON scalars so numeric-looking strings remain distinguishable."""
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    text = value.strip()
    try:
        parsed = json.loads(text)
        return parsed if not isinstance(parsed, (list, dict)) else value
    except Exception:
        return value


def _dump_scalar(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def normalize_operator(value):
    text = clean(value).lower()
    mapping = {">=":"gte", "≥":"gte", "不低于":"gte", "gte":"gte", ">":"gt", "大于":"gt", "gt":"gt", "<=":"lte", "≤":"lte", "不高于":"lte", "lte":"lte", "<":"lt", "小于":"lt", "lt":"lt", "=":"eq", "==":"eq", "eq":"eq", "包含":"text_contains", "等于":"text_equals", "为":"boolean_is", "有无为":"boolean_is", "状态为":"special_is", "special_is":"special_is", "范围位于":"range_inside", "范围相交":"range_overlap"}
    return mapping.get(text, text)


def normalize_value_type(value):
    text = clean(value).lower()
    return {"数值":"number", "数字":"number", "number":"number", "布尔":"boolean", "有无":"boolean", "boolean":"boolean", "ip等级":"ip_grade", "防护等级":"ip_grade", "ip_grade":"ip_grade", "枚举":"enum", "enum":"enum", "文本":"text", "text":"text"}.get(text, text or "number")


def normalize_search_type(value):
    text = clean(value).lower()
    return {
        "自动识别":"auto", "自动":"auto", "auto":"auto",
        "连续数值":"continuous", "连续":"continuous", "continuous":"continuous",
        "整数数值":"integer", "整数":"integer", "integer":"integer",
        "有序离散":"ordered_discrete", "有序枚举":"ordered_discrete", "ordered_discrete":"ordered_discrete",
        "无序枚举":"unordered_enum", "无序离散":"unordered_enum", "unordered_enum":"unordered_enum",
        "布尔开关":"boolean", "布尔":"boolean", "boolean":"boolean",
    }.get(text, text or "auto")


def display_search_type(value):
    return {
        "auto":"自动识别", "continuous":"连续数值", "integer":"整数数值",
        "ordered_discrete":"有序离散", "unordered_enum":"无序枚举", "boolean":"布尔开关",
    }.get(clean(value), clean(value) or "自动识别")


def normalize_preference(value):
    text = clean(value).lower()
    return {"越大越好":"higher", "较大较好":"higher", "higher":"higher", "越小越好":"lower", "较小较好":"lower", "lower":"lower", "中性":"neutral", "无直接偏好":"neutral", "neutral":"neutral"}.get(text, text or "neutral")


def normalize_derivation(value):
    text = clean(value).lower()
    return {"规则判定":"rule", "自动规则":"rule", "rule":"rule", "从种子继承":"inherit", "继承":"inherit", "inherit":"inherit", "人工维护":"manual", "manual":"manual"}.get(text, text or "rule")


def normalize_coupling_type(value):
    text = clean(value).lower()
    return {"正向":"positive", "正相关":"positive", "positive":"positive", "负向":"negative", "负相关":"negative", "negative":"negative", "可行域":"feasible_domain", "条件边界":"feasible_domain", "feasible_domain":"feasible_domain"}.get(text, text)


def normalize_severity(value):
    text = clean(value).lower()
    return {"提示":"info", "信息":"info", "info":"info", "警告":"warning", "warning":"warning", "严重":"error", "阻断":"error", "error":"error"}.get(text, text or "info")


def display_bool(value):
    return "是" if int(value or 0) else "否"


def display_operator(value):
    return {"gte":"≥", "gt":">", "lte":"≤", "lt":"<", "eq":"=", "boolean_is":"为", "special_is":"状态为", "text_equals":"等于", "text_contains":"包含", "range_inside":"范围位于", "range_overlap":"范围相交"}.get(clean(value), clean(value))


def display_value_type(value):
    return {"number":"数值", "boolean":"布尔", "ip_grade":"IP等级", "enum":"枚举", "text":"文本"}.get(clean(value), clean(value))


def display_preference(value):
    return {"higher":"越大越好", "lower":"越小越好", "neutral":"中性"}.get(clean(value), clean(value))


def display_derivation(value):
    return {"rule":"规则判定", "inherit":"从种子继承", "manual":"人工维护"}.get(clean(value), clean(value))


def display_coupling(value):
    return {"positive":"正向", "negative":"负向", "feasible_domain":"可行域"}.get(clean(value), clean(value))


def display_severity(value):
    return {"info":"提示", "warning":"警告", "error":"严重"}.get(clean(value), clean(value))


def normalize_model_kind(value):
    text = clean(value).lower()
    return {"价格":"price", "价格模型":"price", "price":"price", "效能":"effectiveness", "效能模型":"effectiveness", "effectiveness":"effectiveness"}.get(text, text)


def display_model_kind(value):
    return {"price":"价格", "effectiveness":"效能"}.get(clean(value), clean(value))


def normalize_source_type(value):
    text = clean(value).lower()
    return {"产品参数":"product_parameter", "方案参数":"product_parameter", "product_parameter":"product_parameter", "数据库配置值":"configured_value", "configured_value":"configured_value", "模型常量":"constant", "constant":"constant"}.get(text, text)


def display_source_type(value):
    return {"product_parameter":"产品参数", "configured_value":"数据库配置值", "constant":"模型常量"}.get(clean(value), clean(value))


def normalize_model_data_type(value):
    text = clean(value).lower()
    return {"数值":"number", "number":"number", "numeric":"number", "整数":"integer", "integer":"integer", "布尔":"boolean", "boolean":"boolean", "枚举":"enum", "enum":"enum", "文本":"text", "text":"text", "ip等级":"ip_grade", "防护等级":"ip_grade", "ip_grade":"ip_grade"}.get(text, text)


def display_model_data_type(value):
    return {"number":"数值", "numeric":"数值", "integer":"整数", "boolean":"布尔", "enum":"枚举", "text":"文本", "ip_grade":"IP等级"}.get(clean(value), clean(value))


def normalize_missing_policy(value):
    text = clean(value).lower()
    return {
        "缺失时拒绝计算":"reject", "拒绝计算":"reject", "reject":"reject",
        "使用训练均值":"training_mean", "训练均值":"training_mean", "training_mean":"training_mean",
        "使用数据库配置值":"configured_value", "数据库配置值":"configured_value", "configured_value":"configured_value", "default":"configured_value",
        "使用模型常量":"constant", "模型常量":"constant", "constant":"constant",
        "使用零值":"zero", "零值":"zero", "zero":"zero",
        "使用训练众数":"mode", "训练众数":"mode", "training_mode":"mode", "mode":"mode",
    }.get(text, text)


def display_missing_policy(value):
    return {
        "reject":"缺失时拒绝计算", "training_mean":"使用训练均值",
        "configured_value":"使用数据库配置值", "default":"使用数据库配置值",
        "constant":"使用模型常量", "zero":"使用零值", "training_mode":"使用训练众数", "mode":"使用训练众数",
    }.get(clean(value), clean(value))


def _json_allowed(value):
    if value in (None, ""):
        return []
    if isinstance(value, list):
        return value
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, list) else []
    except Exception:
        return [x.strip() for x in re.split(r"[、,，;；|]+", clean(value)) if x.strip()]


def rows_to_dicts(rows):
    rows = [row for row in rows if any(clean(v) for v in row)]
    if not rows:
        return []
    headers = [clean(x) for x in rows[0]]
    result = []
    for row in rows[1:]:
        item = {}
        for i, header in enumerate(headers):
            if header:
                item[header] = row[i] if i < len(row) else ""
        if any(clean(v) for v in item.values()):
            result.append(item)
    return result


def apply_value_mappings(parameters, mapping_rows, errors=None):
    """Apply the authoritative three-layer mapping sheet to parameter rows.

    Shared by full DataMaster imports and partial Product Release maintenance
    workbooks so the two ingestion paths cannot drift semantically.
    """
    errors = errors if errors is not None else []
    param_by_key = dict((clean(item.get("parameter_id")), item)
                        for item in parameters or [] if clean(item.get("parameter_id")))
    grouped = {}
    for row in mapping_rows or []:
        parameter_id = clean(row.get("parameter_id"))
        if parameter_id:
            grouped.setdefault(parameter_id, []).append(row)
    for parameter_id, rows in grouped.items():
        parameter = param_by_key.get(parameter_id)
        if parameter is None:
            errors.append("ValueMappings引用了不存在的指标：%s" % parameter_id)
            continue
        allowed, special, display_mapping, model_mapping = [], [], {}, {}
        for row in rows:
            business_value = _json_scalar(row.get("business_value"))
            key = str(business_value)
            if parse_bool(row.get("allowed", 0)):
                allowed.append(business_value)
            if parse_bool(row.get("special", 0)):
                special.append(key)
            if row.get("display_value") not in (None, ""):
                display_mapping[key] = str(row.get("display_value"))
            if row.get("model_value") not in (None, ""):
                model_mapping[key] = _json_scalar(row.get("model_value"))
        parameter["allowed_values_json"] = json.dumps(allowed, ensure_ascii=False) if allowed else None
        parameter["special_value_keys_json"] = json.dumps(special, ensure_ascii=False) if special else None
        parameter["display_value_mapping_json"] = json.dumps(display_mapping, ensure_ascii=False) if display_mapping else None
        parameter["model_value_mapping_json"] = json.dumps(model_mapping, ensure_ascii=False) if model_mapping else None
    return parameters


SUPPORTED_VALUE_TYPES = ("number", "boolean", "ip_grade", "enum", "text")
SUPPORTED_SEARCH_TYPES = ("auto", "continuous", "integer", "ordered_discrete", "unordered_enum", "boolean")
SUPPORTED_OPERATORS = ("gte", "gt", "lte", "lt", "eq", "boolean_is", "special_is", "text_equals", "text_contains", "range_inside", "range_overlap")
SUPPORTED_COUPLING_TYPES = ("positive", "negative", "feasible_domain")
SUPPORTED_SEVERITIES = ("info", "warning", "error")
OUTPUT_FIELDS = ("__predicted_price_wan", "__capability_score", "__feasibility_probability")

_SECTION_REQUIRED = {
    "products": ("product_code", "product_name"),
    "parameters": ("parameter_id", "label"),
    "parameter_groups": ("group_name",),
    "tags": ("tag_id", "tag_name"),
    "tag_rules": ("rule_id", "tag_id", "parameter_id"),
    "couplings": ("coupling_id", "coupling_name", "parameter_a", "parameter_b"),
    "constraints": ("rule_id", "rule_name", "left_parameter"),
    "agreements": ("agreement_id", "agreement_name"),
}
_SECTION_PK = {
    "products": "product_code",
    "parameters": "parameter_id",
    "parameter_groups": "group_name",
    "tags": "tag_id",
    "tag_rules": "rule_id",
    "couplings": "coupling_id",
    "constraints": "rule_id",
    "agreements": "agreement_id",
}
_SECTION_TITLE = {
    "products": "成品信息", "parameters": "指标定义", "parameter_groups": "指标分组",
    "tags": "标签字典", "tag_rules": "标签规则", "couplings": "耦合关系",
    "constraints": "约束规则", "agreements": "历史协议",
}


def _duplicate_keys(items, key):
    seen = set()
    duplicates = set()
    for item in items:
        value = clean(item.get(key))
        if value in seen:
            duplicates.add(value)
        seen.add(value)
    return sorted(v for v in duplicates if v)


def validate_business_data(data):
    """Structural validator shared by DataMaster preview and product releases.

    Returns ``(errors, warnings)`` for a parsed business-data dict whose keys are
    ``products`` / ``parameters`` / ``tags`` / ``tag_rules`` / ``couplings`` /
    ``constraints`` / ``agreements``.  It checks required fields, duplicate
    primary keys, supported enum values, numeric bounds and cross-section
    references, so a "preview passed" verdict means the data can actually be
    written to SQLite.
    """
    errors = []
    warnings = []

    products = data.get("products") or []
    if len(products) != 1:
        errors.append("成品信息必须且只能有一条。")
    product_code = ""
    for index, item in enumerate(products, 1):
        for field in _SECTION_REQUIRED["products"]:
            if not clean(item.get(field)):
                errors.append("成品信息第%d条缺少%s。" % (index, field))
        if index == 1:
            product_code = clean(item.get("product_code"))

    for section in ("parameters", "parameter_groups", "tags", "tag_rules", "couplings", "constraints", "agreements"):
        items = data.get(section) or []
        title = _SECTION_TITLE[section]
        pk = _SECTION_PK[section]
        required = _SECTION_REQUIRED[section]
        for index, item in enumerate(items, 1):
            for field in required:
                if not clean(item.get(field)):
                    errors.append("%s第%d条缺少%s。" % (title, index, field))
        duplicates = _duplicate_keys(items, pk)
        if duplicates:
            errors.append("%s存在重复编号：%s" % (title, "、".join(duplicates)))

    parameters = data.get("parameters") or []
    param_ids = set(clean(item.get("parameter_id")) for item in parameters if clean(item.get("parameter_id")))
    if not param_ids:
        errors.append("至少需要一条指标定义。")
    for item in parameters:
        pid = clean(item.get("parameter_id"))
        if item.get("value_type") not in SUPPORTED_VALUE_TYPES:
            errors.append("指标%s的取值类型无效：%s。" % (pid, item.get("value_type")))
        if item.get("search_type") not in SUPPORTED_SEARCH_TYPES:
            errors.append("指标%s的搜索类型无效：%s。" % (pid, item.get("search_type")))
        if item.get("min_value") is not None and item.get("max_value") is not None:
            try:
                if float(item["min_value"]) > float(item["max_value"]):
                    errors.append("指标%s的工程下限不能高于工程上限。" % pid)
            except (TypeError, ValueError):
                errors.append("指标%s的工程上下限必须是数字。" % pid)
        if item.get("value_type") == "enum":
            allowed = _json_allowed(item.get("allowed_values_json"))
            raw_mapping = item.get("model_value_mapping_json")
            mapping = {}
            if raw_mapping:
                try:
                    parsed_mapping = json.loads(raw_mapping) if isinstance(raw_mapping, str) else raw_mapping
                    if isinstance(parsed_mapping, dict):
                        mapping = {str(k): v for k, v in parsed_mapping.items()}
                except Exception:
                    pass
            if allowed and mapping:
                missing = [str(v) for v in allowed if str(v).strip() and str(v).strip() not in mapping]
                if missing:
                    warnings.append(
                        "指标「%s」有 %d 个业务可选值，但当前模型值映射只覆盖 %d 个。缺少：%s。请为该业务值填写模型实际使用的编码。"
                        % (item.get("label") or pid, len(allowed), len(mapping), "、".join(missing))
                    )
        # Allowed values must be a JSON array and mappings must be JSON objects.
        raw_allowed = item.get("allowed_values_json")
        if raw_allowed not in (None, ""):
            try:
                parsed_allowed = json.loads(raw_allowed) if isinstance(raw_allowed, str) else raw_allowed
                if not isinstance(parsed_allowed, list):
                    errors.append("指标%s的允许值必须是JSON数组。" % pid)
            except Exception:
                errors.append("指标%s的允许值必须是JSON数组。" % pid)
        raw_mapping = item.get("model_value_mapping_json")
        if raw_mapping not in (None, ""):
            try:
                parsed_mapping = json.loads(raw_mapping) if isinstance(raw_mapping, str) else raw_mapping
                if not isinstance(parsed_mapping, dict):
                    errors.append("指标%s的模型值映射必须是JSON对象。" % pid)
            except Exception:
                errors.append("指标%s的模型值映射必须是JSON对象。" % pid)
        raw_display_mapping = item.get("display_value_mapping_json")
        if raw_display_mapping not in (None, ""):
            try:
                normalize_display_mapping(
                    raw_display_mapping,
                    _json_allowed(item.get("allowed_values_json")) +
                    _json_allowed(item.get("special_value_keys_json")),
                )
            except ValueError as exc:
                errors.append("指标%s：%s" % (pid, exc))
        raw_special = item.get("special_value_keys_json")
        if raw_special not in (None, ""):
            try:
                parsed_special = json.loads(raw_special) if isinstance(raw_special, str) else raw_special
                if not isinstance(parsed_special, list):
                    errors.append("指标%s的特殊业务状态值必须是JSON数组。" % pid)
                elif item.get("value_type") in ("number", "ip_grade", "boolean"):
                    invalid = [value for value in parsed_special if num(value) is None]
                    if invalid:
                        errors.append("指标%s的特殊业务状态值无法转换为数值：%s。" % (pid, invalid[0]))
            except Exception:
                errors.append("指标%s的特殊业务状态值必须是JSON数组。" % pid)

    # Parameter-group existence is a business reference check.
    group_names = set(clean(g.get("group_name")) for g in (data.get("parameter_groups") or []) if clean(g.get("group_name")))
    if group_names:
        for item in parameters:
            g = clean(item.get("parameter_group")) or "其他"
            if g not in group_names:
                errors.append("指标%s引用了不存在的指标分组%s。" % (clean(item.get("parameter_id")), g))

    tag_ids = set(clean(item.get("tag_id")) for item in (data.get("tags") or []) if clean(item.get("tag_id")))
    for item in data.get("tag_rules") or []:
        rule_id = clean(item.get("rule_id"))
        if item.get("tag_id") not in tag_ids:
            errors.append("标签规则%s引用不存在的标签%s。" % (rule_id, item.get("tag_id")))
        if item.get("parameter_id") not in param_ids and item.get("parameter_id") not in OUTPUT_FIELDS:
            errors.append("标签规则%s引用不存在的指标%s。" % (rule_id, item.get("parameter_id")))
        if item.get("operator") not in SUPPORTED_OPERATORS:
            errors.append("标签规则%s的比较关系无效：%s。" % (rule_id, item.get("operator")))

    for item in data.get("couplings") or []:
        coupling_id = clean(item.get("coupling_id"))
        if item.get("coupling_type") not in SUPPORTED_COUPLING_TYPES:
            errors.append("耦合关系%s的关系类型无效：%s。" % (coupling_id, item.get("coupling_type")))
        if item.get("parameter_a") not in param_ids or item.get("parameter_b") not in param_ids:
            errors.append("耦合关系%s引用不存在的指标%s/%s。" % (coupling_id, item.get("parameter_a"), item.get("parameter_b")))
        if item.get("severity") not in SUPPORTED_SEVERITIES:
            errors.append("耦合关系%s的提示级别无效：%s。" % (coupling_id, item.get("severity")))

    for item in data.get("constraints") or []:
        rule_id = clean(item.get("rule_id"))
        if item.get("operator") not in ("gte", "gt", "lte", "lt", "eq"):
            errors.append("约束规则%s的比较关系无效：%s。" % (rule_id, item.get("operator")))
        if item.get("left_parameter") not in param_ids:
            errors.append("约束规则%s引用不存在的指标%s。" % (rule_id, item.get("left_parameter")))
        if item.get("right_parameter") and item.get("right_parameter") not in param_ids:
            errors.append("约束规则%s引用不存在的指标%s。" % (rule_id, item.get("right_parameter")))
        if item.get("severity") not in SUPPORTED_SEVERITIES:
            errors.append("约束规则%s的提示级别无效：%s。" % (rule_id, item.get("severity")))
        if item.get("rule_kind") in ("conditional_lower", "conditional_upper") and item.get("template_metadata_json"):
            try:
                meta = json.loads(item["template_metadata_json"]) if isinstance(item["template_metadata_json"], str) else item["template_metadata_json"]
                if isinstance(meta, dict):
                    controller = meta.get("controller")
                    target = meta.get("target")
                    if controller and controller not in param_ids:
                        errors.append("条件关系%s引用不存在的控制指标%s。" % (rule_id, controller))
                    if target and target not in param_ids:
                        errors.append("条件关系%s引用不存在的从属指标%s。" % (rule_id, target))
            except Exception:
                errors.append("约束规则%s的模板元数据不是有效JSON。" % rule_id)

    for item in data.get("agreements") or []:
        agreement_id = clean(item.get("agreement_id"))
        if product_code and clean(item.get("product_code")) not in ("", product_code):
            errors.append("协议%s的成品代号与草稿不一致。" % agreement_id)
        unknown_tags = sorted(set(item.get("tags") or []) - tag_ids)
        if unknown_tags:
            errors.append("协议%s引用不存在的标签：%s" % (agreement_id, "、".join(unknown_tags)))

    return errors, warnings


class DataMasterService(object):
    def __init__(self, store, runtime):
        self.store = store
        self.runtime = runtime

    @staticmethod
    def _guide_rows():
        return [
            ["工作表", "字段/主题", "应该填写什么", "可选值或格式", "示例", "常见错误"],
            ["通用", "绿色下拉单元格", "请直接从下拉列表选择，不要自行创造近义词。", "以字典_下拉项为准", "是 / 否", "填写1/0、True/False或英文缩写"],
            ["指标定义", "取值类型", "描述协议中这个字段保存的值是什么类型。", "数值、布尔、IP等级、枚举、文本", "自诊断→布尔", "把布尔取值“有/无”填进取值类型"],
            ["指标定义", "搜索类型", "描述智能生成时如何改变该属性。", "自动识别、连续数值、整数数值、有序离散、无序枚举、布尔开关", "防护等级→整数数值；材料→无序枚举", "把质量设置成枚举，或把材料设置成连续数值"],
            ["指标定义", "允许值", "仅离散/枚举字段填写，用顿号分隔合法取值；连续和布尔通常留空。", "值1、值2、值3", "材料：铝合金、钛合金、不锈钢", "布尔字段填写“布尔”；正确取值应在历史协议中填有/无"],
            ["指标定义", "工程下限/上限", "业务工程参考范围；用于默认搜索区间和风险提示，不会阻止、截断或改写用户显式输入。", "数字；下限≤上限", "IP等级：54～68", "把参考范围误当成绝对硬约束；真正硬规则应配置约束规则"],
            ["指标定义", "前端显示映射(JSON)", "仅把canonical业务值显示为操作员文本，不参与存储、筛选、生成或模型编码。", "JSON对象", "{\"0\":\"无\",\"1\":\"有\"}", "复用模型取值映射做界面显示"],
            ["指标定义", "效能方向", "仅表达效能偏好，不代表硬约束。", "越大越好、越小越好、中性", "响应时间→越小越好", "用方向代替约束规则"],
            ["指标定义", "是否必填", "模型或协议计算缺少该值时是否允许继续。", "是、否", "额定载荷→是", "把模型必填字段设为否"],
            ["指标定义", "允许自动调整", "智能生成器是否可以改变这个值。", "是、否", "材料牌号由专家指定→否", "把不可修改的法规字段设为是"],
            ["历史协议", "布尔属性取值", "请填中文业务值。系统导入后自动转换为1/0。", "有、无", "自诊断=有", "填写“布尔”或“boolean”"],
            ["历史协议", "IP等级取值", "可以填写IP前缀或数字；超出DataMaster参考范围仍可保存并在计算时提示。", "IP54 或 54", "IP64", "只允许填写历史出现过的等级"],
            ["标签字典", "生成判定方式", "规则判定表示由标签规则计算；继承表示从参考方案继承；人工维护表示需要专家确认。", "规则判定、从种子继承、人工维护", "高推力→规则判定", "没有规则却选择规则判定"],
            ["标签规则", "同组/多组逻辑", "同一规则组内为AND，不同规则组之间为OR。", "规则组可填default、A、B等", "A组：推力≥12000且质量≤10", "把每条规则都填成不同组，导致全部变成OR"],
            ["标签规则", "条件值", "数值填数字；布尔填有/无；范围条件分别填值1和值2。", "取决于指标类型", "自诊断 为 有", "布尔填1/0，造成维护人员难以理解"],
            ["耦合关系", "关系类型", "正向/负向表示变化趋势；可行域表示明确数值关系。", "正向、负向、可行域", "峰值推力≥1.5×额定推力→可行域", "把弱相关性配置为严重硬约束"],
            ["耦合关系", "提示级别", "提示和警告只展示风险；严重可作为硬约束。", "提示、警告、严重", "历史相关关系→警告", "不确定的经验关系设为严重"],
            ["约束规则", "比较关系", "系统按 左侧指标 ⊙ 系数×右侧指标+偏置 校验。", "≥、>、≤、<、=", "峰值推力≥1.5×额定推力", "左右指标和单位不一致"],
            ["模型字段绑定", "维护边界", "本页仅展示或配置HTTP模型计算输入，不参与业务数据检查，也不阻止成品切换。", "可留空或由服务就绪后同步", "采购批量默认100", "把本页当成业务属性主表"],
            ["模型字段绑定", "缺失策略", "仅在点击价格或效能计算时，由对应HTTP服务契约处理缺失字段。", "缺失时拒绝计算、使用训练均值、使用数据库配置值、使用模型常量、使用零值、使用训练众数", "采购批量→使用训练均值", "为了通过业务数据导入而强行补零"],
            ["成品切换", "业务数据与双模型", "业务数据可先独立导入和切换；成品代号或字段不一致时只暂停推荐计算。", "业务product_code可暂时不同", "AIRCRAFT_DOOR_LOCK", "为适配旧模型而篡改新成品属性"],
        ]

    def _dictionary_sheet(self, snap):
        parameters = [str(item.get("parameter_id")) for item in snap.get("parameters", []) if item.get("parameter_id")]
        tags = [str(item.get("tag_id")) for item in snap.get("tags", []) if item.get("tag_id")]
        active_groups = [str(g.get("group_name")) for g in snap.get("parameter_groups", []) if g.get("group_name") and int(g.get("enabled", 1) or 0) != 0]
        dictionaries = OrderedDict([
            ("是否", ["是", "否"]),
            ("布尔取值", ["有", "无"]),
            ("取值类型", ["数值", "布尔", "IP等级", "枚举", "文本"]),
            ("搜索类型", ["自动识别", "连续数值", "整数数值", "有序离散", "无序枚举", "布尔开关"]),
            ("效能方向", ["越大越好", "越小越好", "中性"]),
            ("指标分组", active_groups),
            ("标签判定方式", ["规则判定", "从种子继承", "人工维护"]),
            ("比较关系", ["≥", ">", "≤", "<", "=", "为", "等于", "包含", "范围位于", "范围相交"]),
            ("耦合类型", ["正向", "负向", "可行域"]),
            ("提示级别", ["提示", "警告", "严重"]),
            ("协议来源", ["历史协议", "专家修改方案", "智能生成方案", "试验方案", "供应商报价"]),
            ("模型类型", ["价格", "效能"]),
            ("字段来源", ["产品参数", "数据库配置值", "模型常量"]),
            ("模型数据类型", ["数值", "整数", "布尔", "IP等级", "枚举", "文本"]),
            ("缺失策略", ["缺失时拒绝计算", "使用训练均值", "使用数据库配置值", "使用模型常量", "使用零值", "使用训练众数"]),
            ("标签编号", tags),
            ("指标编号", parameters),
            ("规则可用指标", parameters + ["__predicted_price_wan", "__capability_score", "__feasibility_probability"]),
        ])
        headers = list(dictionaries.keys())
        height = max([len(values) for values in dictionaries.values()] or [0])
        rows = [headers]
        for index in range(height):
            rows.append([values[index] if index < len(values) else "" for values in dictionaries.values()])
        names = OrderedDict()
        name_map = {
            "是否":"DM_YES_NO", "布尔取值":"DM_BOOLEAN_VALUES", "取值类型":"DM_VALUE_TYPES",
            "搜索类型":"DM_SEARCH_TYPES", "效能方向":"DM_PREFERENCES", "指标分组":"DM_PARAMETER_GROUPS",
            "标签判定方式":"DM_TAG_DERIVATIONS",
            "比较关系":"DM_OPERATORS", "耦合类型":"DM_COUPLING_TYPES", "提示级别":"DM_SEVERITIES",
            "协议来源":"DM_PROTOCOL_SOURCES", "模型类型":"DM_MODEL_KINDS", "字段来源":"DM_SOURCE_TYPES",
            "模型数据类型":"DM_MODEL_DATA_TYPES", "缺失策略":"DM_MISSING_POLICIES",
            "标签编号":"DM_TAG_IDS", "指标编号":"DM_PARAMETER_IDS", "规则可用指标":"DM_RULE_FIELDS",
        }
        for col_index, (header, values) in enumerate(dictionaries.items()):
            if values:
                col = chr(65 + col_index)
                names[name_map[header]] = "'字典_下拉项'!$%s$2:$%s$%d" % (col, col, len(values) + 1)
        return rows, names

    @staticmethod
    def _validation(sqref, formula, title, prompt):
        return {"sqref":sqref, "formula1":formula, "prompt_title":title, "prompt":prompt,
                "error_title":"填写值不在允许范围", "error":"请使用单元格下拉列表中的标准值。"}

    def _validations(self, snap):
        v = self._validation
        result = {
            "成品信息": [v("D2:D100", "DM_YES_NO", "是否启用", "当前版本必须保留一个启用成品。")],
            "指标定义": [
                v("C2:C1000", "DM_PARAMETER_GROUPS", "指标分组", "从已启用的指标分组中选择。"),
                v("E2:E1000", "DM_VALUE_TYPES", "取值类型", "选择字段存储的值类型。布尔字段这里只选“布尔”，具体值在历史协议中填有/无。"),
                v("F2:F1000", "DM_SEARCH_TYPES", "搜索类型", "选择智能生成如何改变该属性。"),
                v("I2:I1000", "DM_PREFERENCES", "效能方向", "这是偏好方向，不是工程硬约束。"),
                v("M2:M1000", "DM_YES_NO", "是否必填", "请直接选择是或否。"),
                v("N2:N1000", "DM_YES_NO", "允许自动调整", "请直接选择是或否。"),
                v("Q2:Q1000", "DM_YES_NO", "是否启用", "停用后该业务属性不再参与数据中心运行；与当前模型是否使用无关。"),
                {"sqref":"S2:S1000", "type":"custom", "formula1":"TRUE",
                 "prompt_title":"前端显示映射(JSON)",
                 "prompt":"格式：JSON对象；示例：{\"0\":\"无\",\"1\":\"有\"}。左边是数据库/业务原值，右边只修改界面显示，不改变历史数据、生成值或模型输入。"},
            ],
            "标签字典": [v("E2:E1000", "DM_TAG_DERIVATIONS", "标签判定方式", "规则判定、从种子继承或人工维护。"), v("G2:G1000", "DM_YES_NO", "是否启用", "停用后不参与推荐，但记录仍保留。")],
            "标签规则": [v("B2:B3000", "DM_TAG_IDS", "标签编号", "从标签字典选择。"), v("C2:C3000", "DM_RULE_FIELDS", "指标编号", "从指标定义或三个模型输出字段中选择。"), v("D2:D3000", "DM_OPERATORS", "比较关系", "布尔字段通常选择“为”，条件值填写有/无。"), v("H2:H3000", "DM_YES_NO", "是否启用", "上级标签停用时规则暂不执行，但仍可保存编辑。")],
            "耦合关系": [v("C2:C2000", "DM_COUPLING_TYPES", "关系类型", "弱经验关系使用正向/负向；明确数学关系使用可行域。"), v("D2:E2000", "DM_PARAMETER_IDS", "指标编号", "从指标定义选择。"), v("F2:F2000", "DM_OPERATORS", "可行域比较", "仅可行域关系需要填写。"), v("J2:J2000", "DM_SEVERITIES", "提示级别", "不确定关系不要配置成严重。"), v("N2:N2000", "DM_YES_NO", "是否启用", "停用后不参与生成和提示。")],
            "约束规则": [v("C2:C2000", "DM_PARAMETER_IDS", "左侧指标", "从指标定义选择。"), v("D2:D2000", "DM_OPERATORS", "比较关系", "按左侧 ⊙ 系数×右侧+偏置计算。"), v("E2:E2000", "DM_PARAMETER_IDS", "右侧指标", "无右侧指标时可留空并使用偏置常数。"), v("H2:H2000", "DM_SEVERITIES", "提示级别", "严重表示工程硬约束。"), v("L2:L2000", "DM_YES_NO", "是否启用", "停用后规则保留但不执行。")],
            "历史协议": [v("D2:D10000", "DM_PROTOCOL_SOURCES", "协议来源", "选择数据来源，便于追溯。")],
            "模型字段绑定": [v("A2:A2000", "DM_MODEL_KINDS", "模型类型", "仅描述价格或效能HTTP服务。"), v("D2:D2000", "DM_SOURCE_TYPES", "字段来源", "仅在模型计算时使用。"), v("E2:E2000", "DM_MODEL_DATA_TYPES", "数据类型", "由对应HTTP服务在计算时解释。"), v("G2:G2000", "DM_YES_NO", "是否必填", "仅表示模型计算契约，不影响业务数据导入。"), v("H2:H2000", "DM_MISSING_POLICIES", "缺失策略", "由对应HTTP服务在计算时处理。"), v("L2:L2000", "DM_YES_NO", "是否启用", "不参与业务数据检查与切换。")],
        }
        # Historical boolean parameters receive user-friendly 有/无 drop-downs.
        for offset, parameter in enumerate(snap.get("parameters", [])):
            if parameter.get("value_type") == "boolean":
                col_index = 8 + offset
                col = ""
                number = col_index + 1
                while number:
                    number, rem = divmod(number - 1, 26)
                    col = chr(65 + rem) + col
                result["历史协议"].append(v("%s2:%s10000" % (col, col), "DM_BOOLEAN_VALUES", parameter.get("label") or "布尔属性", "布尔取值请填写有或无，系统内部自动转换为1或0。"))
            elif parameter.get("value_type") == "enum":
                allowed = _json_allowed(parameter.get("allowed_values_json"))
                if allowed and len(",".join(str(x) for x in allowed)) <= 240:
                    col_index = 8 + offset
                    col = ""
                    number = col_index + 1
                    while number:
                        number, rem = divmod(number - 1, 26)
                        col = chr(65 + rem) + col
                    result["历史协议"].append(v("%s2:%s10000" % (col, col), '"%s"' % ",".join(str(x) for x in allowed), parameter.get("label") or "枚举属性", "请从允许值中选择。"))
        return result

    def _base_sheets(self, empty=False, snapshot=None):
        snap = snapshot if snapshot is not None else self.store.admin_snapshot()
        product = snap["products"][0] if snap["products"] else {"product_code": "NEW_PRODUCT", "product_name": "新成品", "product_description": "", "enabled": 1}
        dictionary_rows, _defined_names = self._dictionary_sheet(snap)
        rows = [("填写说明", self._guide_rows()), ("字典_下拉项", dictionary_rows)]
        onboarding = snap.get("onboarding") or {}
        inference_rows = onboarding.get("inference_rows") or []
        if inference_rows:
            report_headers = ["原始列名", "字段编号", "字段名称", "推断类型", "搜索类型", "是否必填", "有效值数", "缺失数", "不同值数", "置信状态", "推断说明"]
            report = [report_headers]
            for item in inference_rows:
                report.append([
                    item.get("source_header"), item.get("parameter_id"), item.get("label"),
                    display_value_type(item.get("value_type")), display_search_type(item.get("search_type")),
                    display_bool(item.get("required")), item.get("observed_count"), item.get("missing_count"),
                    item.get("unique_count"), "需要人工确认" if item.get("confidence") == "needs_confirmation" else "高",
                    item.get("note"),
                ])
            rows.append(("自动推断报告", report))
        rows.append(("成品信息", [SHEETS["成品信息"], [product.get("product_code"), product.get("product_name"), product.get("product_description"), display_bool(product.get("enabled", 1))]]))
        params = []
        for p in snap["parameters"]:
            params.append([p.get("parameter_id"), p.get("label"), p.get("parameter_group", "其他"), p.get("unit"), display_value_type(p.get("value_type")), display_search_type(p.get("search_type", "auto")), p.get("min_value"), p.get("max_value"), display_preference(p.get("preference")), p.get("description"), p.get("adjustment_hint"), "、".join(str(x) for x in _json_allowed(p.get("allowed_values_json"))), display_bool(p.get("required", 1)), display_bool(p.get("auto_adjustable", 1)), p.get("decimal_places", 3), p.get("display_order"), display_bool(p.get("enabled", 1)), p.get("model_value_mapping_json") or "", p.get("display_value_mapping_json") or "", p.get("special_value_keys_json") or "", p.get("observed_min"), p.get("observed_max")])
        rows.append(("指标定义", [SHEETS["指标定义"]] + params))
        mapping_rows = []
        for p in snap["parameters"]:
            allowed = _json_allowed(p.get("allowed_values_json"))
            special = _json_allowed(p.get("special_value_keys_json"))
            try:
                display_mapping = json.loads(p.get("display_value_mapping_json") or "{}")
            except Exception:
                display_mapping = {}
            try:
                model_mapping = json.loads(p.get("model_value_mapping_json") or "{}")
            except Exception:
                model_mapping = {}
            ordered = []
            for value in allowed + special + list(display_mapping.keys()) + list(model_mapping.keys()):
                marker = str(value)
                if marker not in [str(x) for x in ordered]:
                    ordered.append(value)
            for value in ordered:
                key = str(value)
                mapping_rows.append([
                    p.get("parameter_id"), _dump_scalar(value), display_mapping.get(key, ""),
                    _dump_scalar(model_mapping[key]) if key in model_mapping else "",
                    1 if any(str(x) == key for x in allowed) else 0,
                    1 if any(str(x) == key for x in special) else 0,
                ])
        rows.append(("ValueMappings", [SHEETS["ValueMappings"]] + ([] if empty else mapping_rows)))
        groups = []
        for g in snap.get("parameter_groups", []):
            groups.append([g.get("group_name"), g.get("display_order"), g.get("description"), display_bool(g.get("enabled", 1)), display_bool(g.get("default_collapsed", 0))])
        rows.append(("指标分组", [SHEETS["指标分组"]] + ([] if empty else groups)))
        tags = []
        for t in snap["tags"]:
            tags.append([t.get("tag_id"), t.get("tag_name"), t.get("tag_group"), t.get("weight"), display_derivation(t.get("derivation_mode", "rule")), t.get("description"), display_bool(t.get("enabled", 1))])
        rows.append(("标签字典", [SHEETS["标签字典"]] + ([] if empty else tags)))
        trules = []
        for r in snap.get("tag_rules", []):
            trules.append([r.get("rule_id"), r.get("tag_id"), r.get("parameter_id"), display_operator(r.get("operator")), r.get("value1"), r.get("value2"), r.get("rule_group", "default"), display_bool(r.get("enabled", 1))])
        rows.append(("标签规则", [SHEETS["标签规则"]] + ([] if empty else trules)))
        couplings = []
        for c in snap["couplings"]:
            couplings.append([c.get("coupling_id"), c.get("coupling_name"), display_coupling(c.get("coupling_type")), c.get("parameter_a"), c.get("parameter_b"), display_operator(c.get("domain_operator")), c.get("multiplier"), c.get("offset"), c.get("strength"), display_severity(c.get("severity")), c.get("description"), c.get("rationale"), c.get("display_order"), display_bool(c.get("enabled", 1))])
        rows.append(("耦合关系", [SHEETS["耦合关系"]] + ([] if empty else couplings)))
        constraints = []
        for r in snap["constraints"]:
            constraints.append([r.get("rule_id"), r.get("rule_name"), r.get("left_parameter"), display_operator(r.get("operator")), r.get("right_parameter"), r.get("multiplier"), r.get("offset"), display_severity(r.get("severity")), r.get("message"), r.get("rationale"), r.get("display_order"), display_bool(r.get("enabled", 1)), r.get("rule_kind", "affine"), r.get("constraint_group"), r.get("template_metadata_json")])
        rows.append(("约束规则", [SHEETS["约束规则"]] + ([] if empty else constraints)))
        protocol_headers = list(SHEETS["历史协议"]) + [p.get("label") + (("(%s)" % p.get("unit")) if p.get("unit") else "") for p in snap["parameters"]]
        protocols = []
        if not empty:
            tag_name = dict((t["tag_id"], t["tag_name"]) for t in snap["tags"])
            for a in snap["agreements"]:
                values = [a.get("agreement_id"), a.get("agreement_name"), a.get("positioning"), a.get("agreement_source"), a.get("source_year"), a.get("supplier_type"), a.get("historical_price_wan"), "、".join(tag_name.get(x, x) for x in (a.get("tags") or []))]
                for p in snap["parameters"]:
                    value = (a.get("params") or {}).get(p["parameter_id"])
                    if p.get("value_type") == "boolean" and value not in (None, ""):
                        value = "有" if int(float(value)) else "无"
                    elif p.get("value_type") == "ip_grade" and value not in (None, ""):
                        value = "IP%s" % (int(float(value)) if float(value).is_integer() else value)
                    values.append(value)
                protocols.append(values)
        rows.append(("历史协议", [protocol_headers] + protocols))
        bindings = []
        for b in snap.get("model_inputs", []):
            bindings.append([display_model_kind(b.get("model_kind")), b.get("parameter_id"), b.get("label"), display_source_type(b.get("source_type")), display_model_data_type(b.get("data_type")), b.get("unit"), display_bool(b.get("required")), display_missing_policy(b.get("missing_policy")), b.get("configured_value"), b.get("training_mean"), b.get("model_version"), display_bool(b.get("enabled", 1))])
        rows.append(("模型字段绑定", [SHEETS["模型字段绑定"]] + bindings))
        return rows

    def export_current(self):
        snap = self.store.admin_snapshot()
        _rows, names = self._dictionary_sheet(snap)
        return write_workbook_bytes(self._base_sheets(empty=False), validations=self._validations(snap), defined_names=names)

    def export_snapshot(self, snapshot):
        """Export a release draft without requiring it to be active or model-valid."""
        snap = dict(snapshot or {})
        for key in ("products", "parameters", "parameter_groups", "tags", "tag_rules", "couplings", "constraints", "agreements", "model_inputs"):
            snap.setdefault(key, [])
        _rows, names = self._dictionary_sheet(snap)
        return write_workbook_bytes(
            self._base_sheets(empty=False, snapshot=snap),
            validations=self._validations(snap), defined_names=names,
        )

    def template(self):
        snap = self.store.admin_snapshot()
        _rows, names = self._dictionary_sheet(snap)
        return write_workbook_bytes(self._base_sheets(empty=True), validations=self._validations(snap), defined_names=names)

    def parse(self, filename, data):
        if not clean(filename).lower().endswith(".xlsx"):
            raise ValueError("DataMaster仅支持.xlsx工作簿。")
        workbook = read_workbook_bytes(data)
        missing_sheets = [name for name in SHEETS if name not in workbook and name not in OPTIONAL_SHEETS]
        if missing_sheets:
            raise ValueError("DataMaster缺少工作表：%s" % "、".join(missing_sheets))
        parsed = {name: rows_to_dicts(workbook[name]) if name in workbook else [] for name in SHEETS}
        report = {
            "filename": filename, "valid": True, "errors": [], "warnings": [], "counts": {}, "data": {},
            "scope": "business_data_only", "model_contract_checked": False,
        }
        try:
            products = []
            for r in parsed["成品信息"]:
                products.append({"product_code": clean(r.get("成品代号")), "product_name": clean(r.get("成品名称")), "product_description": clean(r.get("成品说明")), "enabled": parse_bool(r.get("是否启用", 1))})
            if len(products) != 1:
                report["errors"].append("当前版本每个DataMaster必须且只能包含一个成品。")
            report["data"]["products"] = products

            parameters = []
            for r in parsed["指标定义"]:
                allowed = clean(r.get("允许值"))
                if allowed:
                    try:
                        parsed_allowed = json.loads(allowed)
                        if not isinstance(parsed_allowed, list):
                            raise ValueError("允许值JSON必须是数组")
                    except Exception:
                        parsed_allowed = [x.strip() for x in re.split(r"[、,，;；|]+", allowed) if x.strip()]
                    allowed = json.dumps(parsed_allowed, ensure_ascii=False)
                value_type = normalize_value_type(r.get("取值类型"))
                search_type = normalize_search_type(r.get("搜索类型"))
                # IP protection grades use an integer engineering domain. Legacy
                # files may contain only observed values (for example IP54/IP65),
                # which must not be interpreted as an exhaustive legal list.
                if value_type == "ip_grade" and search_type == "auto":
                    search_type = "integer"
                    allowed = None
                mapping_text = clean(r.get("模型取值映射(JSON)"))
                mapping = None
                if mapping_text:
                    parsed_mapping = json.loads(mapping_text)
                    if not isinstance(parsed_mapping, dict):
                        raise ValueError("指标%s的模型取值映射必须是JSON对象。" % clean(r.get("指标编号")))
                    mapping = json.dumps(parsed_mapping, ensure_ascii=False)
                special_text = clean(r.get("特殊业务状态值(JSON数组)"))
                special_keys = None
                parsed_special = []
                if special_text:
                    parsed_special = json.loads(special_text)
                    if not isinstance(parsed_special, list):
                        raise ValueError("指标%s的特殊业务状态值必须是JSON数组。" % clean(r.get("指标编号")))
                    special_keys = json.dumps([str(value) for value in parsed_special], ensure_ascii=False)
                display_mapping_text = clean(r.get("前端显示映射(JSON)"))
                display_mapping = None
                if display_mapping_text:
                    try:
                        display_mapping = dump_display_mapping(
                            display_mapping_text, _json_allowed(allowed) + parsed_special
                        )
                    except ValueError as exc:
                        raise ValueError("指标%s：%s" % (clean(r.get("指标编号")), exc))
                parameters.append({
                    "parameter_id": clean(r.get("指标编号")), "label": clean(r.get("指标名称")),
                    "parameter_group": clean(r.get("指标分组")) or "其他", "unit": clean(r.get("单位")),
                    "value_type": value_type, "search_type": search_type, "min_value": num(r.get("工程下限")), "max_value": num(r.get("工程上限")),
                    "observed_min": num(r.get("历史观测下限")), "observed_max": num(r.get("历史观测上限")),
                    "preference": normalize_preference(r.get("效能方向")), "description": clean(r.get("指标说明")), "adjustment_hint": clean(r.get("调整提示")),
                    "allowed_values_json": allowed or None, "model_value_mapping_json": mapping,
                    "display_value_mapping_json": display_mapping,
                    "special_value_keys_json": special_keys,
                    "required": parse_bool(r.get("是否必填", 1)), "auto_adjustable": parse_bool(r.get("允许自动调整", 1)),
                    "decimal_places": integer(r.get("显示小数位"), 3), "display_order": integer(r.get("显示顺序"), len(parameters)+1), "enabled": parse_bool(r.get("是否启用", 1)), "model_bound": 1,
                })
            valid_search_types = set(("auto", "continuous", "integer", "ordered_discrete", "unordered_enum", "boolean"))
            for p in parameters:
                if p.get("search_type") not in valid_search_types:
                    report["errors"].append("指标%s的搜索类型无效：%s" % (p.get("parameter_id"), p.get("search_type")))
                allowed_values = _json_allowed(p.get("allowed_values_json"))
                if p.get("search_type") in ("ordered_discrete", "unordered_enum") and not allowed_values:
                    report["errors"].append("指标%s配置为离散搜索类型时必须填写允许值。" % p.get("parameter_id"))
                if p.get("search_type") == "boolean" and p.get("value_type") != "boolean":
                    report["errors"].append("指标%s只有布尔取值类型才能使用布尔开关搜索。" % p.get("parameter_id"))
                if p.get("search_type") in ("continuous", "integer", "ordered_discrete") and p.get("value_type") not in ("number", "ip_grade"):
                    report["errors"].append("指标%s的搜索类型与取值类型不匹配。" % p.get("parameter_id"))
            for parameter in parameters:
                # Model-bound status is synchronized later from whichever HTTP
                # services are active; it is not a DataMaster validation input.
                parameter["model_bound"] = 0
            report["data"]["parameters"] = parameters

            # A readable ValueMappings sheet is authoritative when present.
            # Legacy JSON columns remain fully supported when it is absent.
            if "ValueMappings" in workbook:
                apply_value_mappings(parameters, parsed["ValueMappings"], report["errors"])

            group_items = []
            if "指标分组" in workbook:
                seen_group_names = set()
                for r in parsed["指标分组"]:
                    name = clean(r.get("分组名称")) or "其他"
                    if name in seen_group_names:
                        continue
                    seen_group_names.add(name)
                    group_items.append({
                        "group_name": name,
                        "display_order": integer(r.get("显示顺序"), len(group_items) + 1),
                        "description": clean(r.get("说明")),
                        "enabled": parse_bool(r.get("是否启用", 1)),
                        "default_collapsed": parse_bool(r.get("默认折叠", 0)),
                    })
            derived_group_names = []
            for p in parameters:
                g = p.get("parameter_group") or "其他"
                if g not in derived_group_names:
                    derived_group_names.append(g)
            if "其他" not in derived_group_names:
                derived_group_names.append("其他")
            if "指标分组" not in workbook:
                # Legacy workbook without a group master sheet: derive from
                # parameter definitions to stay backward compatible.
                group_items = [{"group_name": g, "display_order": i + 1, "description": "", "enabled": 1, "default_collapsed": 0} for i, g in enumerate(derived_group_names)]
            if "指标分组" in workbook:
                # If a group master sheet is present (even empty), do NOT silently
                # create unknown groups; report unknown references directly.
                existing_group_names = set(x["group_name"] for x in group_items)
                for p in parameters:
                    g = p.get("parameter_group") or "其他"
                    if g not in existing_group_names:
                        report["errors"].append("指标%s引用了不存在的指标分组%s。" % (p.get("parameter_id"), g))
            report["data"]["parameter_groups"] = group_items

            tags = []
            for r in parsed["标签字典"]:
                tags.append({"tag_id":clean(r.get("标签编号")), "tag_name":clean(r.get("标签名称")), "tag_group":clean(r.get("标签分组")), "weight":num(r.get("匹配权重"), False), "derivation_mode":normalize_derivation(r.get("生成判定方式")), "description":clean(r.get("标签说明")), "enabled":parse_bool(r.get("是否启用",1))})
            report["data"]["tags"] = tags
            tag_ids = set(x["tag_id"] for x in tags)

            tag_rules = []
            for r in parsed["标签规则"]:
                item={"rule_id":clean(r.get("规则编号")), "tag_id":clean(r.get("标签编号")), "parameter_id":clean(r.get("指标编号")), "operator":normalize_operator(r.get("比较关系")), "value1":clean(r.get("条件值1")), "value2":clean(r.get("条件值2")), "rule_group":clean(r.get("规则组")) or "default", "enabled":parse_bool(r.get("是否启用",1))}
                if item["tag_id"] not in tag_ids: report["errors"].append("标签规则%s引用未知标签%s" % (item["rule_id"], item["tag_id"]))
                if item["parameter_id"] not in param_by_key and item["parameter_id"] not in ("__predicted_price_wan", "__capability_score", "__feasibility_probability"): report["errors"].append("标签规则%s引用未知指标%s" % (item["rule_id"], item["parameter_id"]))
                tag_rules.append(item)
            report["data"]["tag_rules"] = tag_rules

            couplings=[]
            for r in parsed["耦合关系"]:
                item={"coupling_id":clean(r.get("关系编号")), "coupling_name":clean(r.get("关系名称")), "coupling_type":normalize_coupling_type(r.get("关系类型")), "parameter_a":clean(r.get("指标A")), "parameter_b":clean(r.get("指标B")), "domain_operator":normalize_operator(r.get("可行域比较")), "multiplier":num(r.get("系数")), "offset":num(r.get("偏置")), "strength":num(r.get("作用强度")), "severity":normalize_severity(r.get("提示级别")), "description":clean(r.get("关系说明")), "rationale":clean(r.get("设置依据")), "display_order":integer(r.get("显示顺序"),len(couplings)+1), "enabled":parse_bool(r.get("是否启用",1))}
                if item["parameter_a"] not in param_by_key or item["parameter_b"] not in param_by_key:
                    report["errors"].append("耦合关系%s引用未知指标%s/%s" % (item["coupling_id"], item["parameter_a"], item["parameter_b"]))
                couplings.append(item)
            report["data"]["couplings"] = couplings
            constraints=[]
            for r in parsed["约束规则"]:
                item={"rule_id":clean(r.get("规则编号")), "rule_name":clean(r.get("规则名称")), "left_parameter":clean(r.get("左侧指标")), "operator":normalize_operator(r.get("比较关系")), "right_parameter":clean(r.get("右侧指标")) or None, "multiplier":num(r.get("系数")) if clean(r.get("系数")) else 1.0, "offset":num(r.get("偏置")) if clean(r.get("偏置")) else 0.0, "severity":normalize_severity(r.get("提示级别")) or "warning", "message":clean(r.get("违反提示")), "rationale":clean(r.get("设置依据")), "display_order":integer(r.get("显示顺序"),len(constraints)+1), "enabled":parse_bool(r.get("是否启用",1)), "rule_kind":clean(r.get("规则类型")) or "affine", "constraint_group":clean(r.get("约束组")) or None, "template_metadata_json":clean(r.get("模板元数据(JSON)")) or None}
                if item["left_parameter"] not in param_by_key or (item["right_parameter"] and item["right_parameter"] not in param_by_key):
                    report["errors"].append("约束规则%s引用未知指标%s/%s" % (item["rule_id"], item["left_parameter"], item["right_parameter"] or "常数"))
                constraints.append(item)
            report["data"]["constraints"] = constraints

            # Historical protocol parsing uses the same definitions as the UI wide-table import.
            protocol_rows = workbook["历史协议"]
            if len(protocol_rows) > 1:
                from .wide_import import WideTableParser
                tag_stub = [{"tag_id":x["tag_id"],"tag_name":x["tag_name"]} for x in tags]
                # Re-encode only this sheet through the generic CSV parser for one source of truth.
                import csv, io
                out=io.StringIO(); writer=csv.writer(out); writer.writerows(protocol_rows)
                maintenance_parameters = [dict(item, required=0, model_bound=0) for item in parameters]
                fallback_code = products[0]["product_code"] if products else "UNASSIGNED_PRODUCT"
                parsed_protocols=WideTableParser(maintenance_parameters,tag_stub,fallback_code).parse("历史协议.csv",("\ufeff"+out.getvalue()).encode("utf-8"))
                report["warnings"].extend(parsed_protocols.get("global_warnings") or [])
                for row in parsed_protocols["rows"]:
                    if not row["valid"]: report["errors"].append("历史协议第%d行：%s" % (row["row_number"], "；".join(row["errors"])))
                report["data"]["agreements"]=[row["item"] for row in parsed_protocols["rows"] if row["valid"]]
            else:
                report["data"]["agreements"]=[]

            bindings=[]
            for r in parsed["模型字段绑定"]:
                model_kind = normalize_model_kind(r.get("模型类型"))
                parameter_id = clean(r.get("字段编号"))
                item={"binding_id":"%s:%s" % (model_kind, parameter_id), "model_kind":model_kind, "parameter_id":parameter_id, "label":clean(r.get("字段名称")), "source_type":normalize_source_type(r.get("字段来源")), "data_type":normalize_model_data_type(r.get("数据类型")), "unit":clean(r.get("单位")), "required":parse_bool(r.get("是否必填",1)), "missing_policy":normalize_missing_policy(r.get("缺失策略")), "configured_value":clean(r.get("数据库配置值")) or None, "training_mean":num(r.get("训练均值")), "model_version":clean(r.get("模型版本")), "enabled":parse_bool(r.get("是否启用",1))}
                bindings.append(item)
            report["data"]["model_inputs"] = bindings
        except Exception as exc:
            report["errors"].append(str(exc))
        # Shared structural validation: duplicates / required fields / enum
        # values / references.  This makes DataMaster preview agree with
        # product-release validation and with what SQLite can actually accept.
        shared_errors, shared_warnings = validate_business_data(report.get("data", {}))
        for error in shared_errors:
            if error not in report["errors"]:
                report["errors"].append(error)
        report["warnings"].extend(shared_warnings)
        for key, values in report.get("data", {}).items(): report["counts"][key]=len(values)
        report["warnings"].insert(0, "DataMaster只检查业务数据结构和本地引用；当前价格/效能HTTP服务不参与本次检查。")
        report["valid"] = not report["errors"]
        return report

    def commit(self, report):
        if not report.get("valid"):
            raise ValueError("DataMaster校验未通过，不能写入数据库。")
        return self.store.replace_from_datamaster(
            report["data"], evaluate_agreements=False, sync_model_contract=False,
        )
