# -*- coding: utf-8 -*-
"""Infer a maintainable product draft from an ordinary historical wide table.

This module deliberately has no model dependency.  It turns raw customer data
into a draft that operators can correct before model validation and activation.
"""
from __future__ import print_function

import json
import re

from .wide_import import read_table_bytes, split_tags
from .product_table import product_table_rows
from .model_field_types import canonical_field_id


def _clean(value):
    return str(value if value is not None else "").strip()


def _header_key(value):
    return re.sub(r"[\s_\-（）()\[\]【】]+", "", _clean(value)).lower()


ID_HEADERS = set(_header_key(x) for x in (
    "成品编号", "产品编号", "样本编号", "记录编号", "协议编号", "技术协议编号",
    "成品代号", "产品代号", "id", "product_id", "sample_id", "agreement_id", "sku",
))
NAME_HEADERS = set(_header_key(x) for x in (
    "成品名称", "产品名称", "样本名称", "记录名称", "协议名称", "技术协议名称",
    "name", "product_name", "sample_name", "agreement_name",
))
PRICE_HEADERS = set(_header_key(x) for x in (
    "价格", "成品价格", "产品价格", "历史价格", "历史价格万元", "报价", "成交价",
    "price", "historical_price", "historical_price_wan",
))
TAG_HEADERS = set(_header_key(x) for x in ("标签", "标签名称", "tags", "tag"))
YEAR_HEADERS = set(_header_key(x) for x in ("年份", "来源年份", "生产年份", "year", "source_year"))
SUPPLIER_HEADERS = set(_header_key(x) for x in ("供应商", "供应方", "供应方类型", "supplier", "supplier_type"))
POSITION_HEADERS = set(_header_key(x) for x in ("定位", "方案定位", "positioning"))

TRUE_WORDS = set(("1", "true", "yes", "y", "有", "是", "启用", "具备", "支持"))
FALSE_WORDS = set(("0", "false", "no", "n", "无", "否", "停用", "不具备", "不支持"))
BOOLEAN_HEADER = re.compile(r"(^is[_\-]|^has[_\-]|是否|有无|可否|启用|支持|具备|开关|布尔|标志|flag|enabled)", re.I)
TEXT_HEADER = re.compile(r"(编号|代号|名称|备注|说明|描述|序列|批次|code|name|note|description|serial)", re.I)


def parse_missing_tokens(value):
    if isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        raw = re.split(r"[,，;；|]", _clean(value))
    result = []
    for item in raw:
        token = _clean(item)
        if token and token not in result:
            result.append(token)
    return result


def _numeric_token(value):
    text = _clean(value).replace(",", "")
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def _is_missing(value, tokens):
    text = _clean(value)
    if not text:
        return True
    lowered = text.lower()
    for token in tokens:
        if lowered == token.lower():
            return True
        left = _numeric_token(text)
        right = _numeric_token(token)
        if left is not None and right is not None and left == right:
            return True
    return False


def _label_and_unit(header):
    text = _clean(header)
    match = re.match(r"^(.*?)[（(]\s*([^（）()]+)\s*[）)]$", text)
    if not match:
        return text, ""
    return _clean(match.group(1)), _clean(match.group(2))


def _parameter_id(header, index, used):
    label, _unit = _label_and_unit(header)
    return canonical_field_id(label, index, used)


def _decimal_places(values):
    places = 0
    for value in values:
        text = _clean(value).lower()
        if "e" in text:
            places = max(places, 3)
        elif "." in text:
            places = max(places, len(text.rstrip("0").split(".", 1)[1]))
    return min(max(places, 0), 6)


def _generation_range(values):
    """Widen observed min/max into a default generation (engineering) bound.

    Auto-inference only knows the observed range.  Treating that as the hard
    engineering bound would block the extrapolation the recommendation engine
    is designed for, so the generation bound is widened by default and the
    operator is expected to confirm the real engineering limits.
    """
    lo = min(values)
    hi = max(values)
    span = hi - lo
    pad = 0.25 * span if span > 1e-12 else max(abs(lo) * 0.25, 1.0)
    return lo - pad, hi + pad


def _infer_parameter(header, parameter_id, values, missing_tokens, order):
    label, unit = _label_and_unit(header)
    missing_count = sum(1 for value in values if _is_missing(value, missing_tokens))
    observed = [_clean(value) for value in values if not _is_missing(value, missing_tokens)]
    normalized = [value.lower() for value in observed]
    unique = []
    for value in observed:
        if value not in unique:
            unique.append(value)
    required = 0 if missing_count else 1
    confidence = "high"
    note = "根据历史成品取值自动识别；发布前可在数据中心修改。"
    value_type = "text"
    search_type = "auto"
    allowed = []
    minimum = maximum = None
    observed_min = observed_max = None
    decimals = 3

    if observed and all(re.match(r"^ip\s*\d+(?:\.\d+)?$", value, re.I) for value in observed):
        numbers = [float(re.sub(r"^ip\s*", "", value, flags=re.I)) for value in observed]
        value_type, search_type = "ip_grade", "integer"
        observed_min, observed_max = min(numbers), max(numbers)
        minimum, maximum = _generation_range(numbers)
        decimals = 0
        note = "识别为IP防护等级；经验范围取历史样本，工程/生成边界已外扩，请确认工程边界。"
    elif observed and set(normalized).issubset(TRUE_WORDS | FALSE_WORDS) and not set(normalized).issubset(set(("0", "1"))):
        value_type, search_type, decimals = "boolean", "boolean", 0
        note = "历史值包含有/无或是/否语义，已识别为布尔属性。"
    else:
        numbers = [_numeric_token(value) for value in observed]
        all_numeric = bool(observed) and all(value is not None for value in numbers)
        binary_numeric = all_numeric and set(numbers).issubset(set((0.0, 1.0)))
        if binary_numeric and BOOLEAN_HEADER.search(label):
            value_type, search_type, decimals = "boolean", "boolean", 0
            note = "列名具有是否/有无语义，0/1已识别为无/有；请在草稿中确认。"
        elif binary_numeric:
            value_type, search_type, decimals = "enum", "unordered_enum", 0
            allowed = ["0", "1"]
            confidence = "needs_confirmation"
            note = "0/1含义不明确，暂按类型1/类型2的二元枚举保留，不能自动解释为无/有。"
        elif all_numeric:
            ints = all(float(value).is_integer() for value in numbers)
            value_type = "number"
            search_type = "integer" if ints else "continuous"
            observed_min, observed_max = min(numbers), max(numbers)
            minimum, maximum = _generation_range(numbers)
            decimals = 0 if ints else _decimal_places(observed)
            note = "识别为%s；经验范围取历史样本，工程/生成边界已外扩，请确认工程边界。" % ("整数" if ints else "连续数值")
        elif observed and not TEXT_HEADER.search(label) and len(unique) <= 20:
            value_type, search_type = "enum", "unordered_enum"
            allowed = unique
            note = "不同取值数量较少，暂识别为无序枚举。"
        else:
            value_type, search_type = "text", "auto"
            note = "识别为文本；系统不会对该字段做数值搜索。"

    if not observed:
        confidence = "needs_confirmation"
        note = "整列均为空或缺失标识，暂按文本保留，需要人工确认字段类型。"
    if missing_count:
        note += " 检测到%d条缺失，因此自动设为非必填。" % missing_count

    return {
        "parameter_id": parameter_id,
        "label": label or header,
        "unit": unit,
        "value_type": value_type,
        "search_type": search_type,
        "min_value": minimum,
        "max_value": maximum,
        "observed_min": observed_min,
        "observed_max": observed_max,
        "preference": "neutral",
        "description": note,
        "adjustment_hint": "请结合工程含义确认自动推断结果。" if confidence != "high" else "",
        "allowed_values_json": json.dumps(allowed, ensure_ascii=False) if allowed else None,
        "model_value_mapping_json": None,
        "display_value_mapping_json": None,
        "special_value_keys_json": None,
        "required": required,
        "auto_adjustable": 0 if value_type == "text" else 1,
        "decimal_places": decimals,
        "display_order": order,
        "enabled": 1,
        "model_bound": 0,
        "inference": {
            "source_header": header,
            "observed_count": len(observed),
            "missing_count": missing_count,
            "unique_count": len(unique),
            "confidence": confidence,
            "note": note,
        },
    }


def _typed_value(value, parameter, missing_tokens):
    if _is_missing(value, missing_tokens):
        return None
    text = _clean(value)
    value_type = parameter.get("value_type")
    if value_type == "boolean":
        lowered = text.lower()
        if lowered in TRUE_WORDS:
            return 1
        if lowered in FALSE_WORDS:
            return 0
        return int(float(text))
    if value_type == "ip_grade":
        number = float(re.sub(r"^ip\s*", "", text, flags=re.I))
        return int(number) if number.is_integer() else number
    if value_type == "number":
        number = float(text.replace(",", ""))
        return int(number) if number.is_integer() else number
    return text


def _price_value(value, header, missing_tokens, warnings):
    if _is_missing(value, missing_tokens):
        return None
    number = _numeric_token(value)
    if number is None:
        if not any("价格列存在非数字" in item for item in warnings):
            warnings.append("价格列存在非数字内容，相关行已暂时留空，请在草稿中补充。")
        return None
    key = _header_key(header)
    if "万元" in _clean(header) or "wan" in key:
        return number
    if "千元" in _clean(header):
        return number / 10.0
    if "元" in _clean(header):
        return number / 10000.0
    if not any("价格列没有明确单位" in item for item in warnings):
        warnings.append("价格列没有明确单位，暂按万元保存；请在草稿中确认。")
    return number


class HistoricalProductOnboarding(object):
    def analyze(self, filename, raw, product_code, product_name, missing_tokens=None, require_dual_header=False):
        table = product_table_rows(filename, raw, require_dual_header=require_dual_header)
        headers = [_clean(value) for value in table["labels"]]
        field_ids = table["field_ids"]
        if not any(headers):
            raise ValueError("历史成品表表头为空。")
        normalized_headers = [_header_key(value) for value in (field_ids or headers) if value]
        duplicate_headers = sorted(set(value for value in normalized_headers if normalized_headers.count(value) > 1))
        if duplicate_headers:
            raise ValueError("历史成品表存在重复列名：%s" % "、".join(duplicate_headers))
        code = _clean(product_code)
        name = _clean(product_name) or code
        if not code:
            raise ValueError("请填写本批历史成品所属的成品代号。")
        tokens = parse_missing_tokens(missing_tokens)
        data_rows = table["rows"]
        width = len(headers)
        padded = [list(row) + [""] * max(0, width - len(row)) for row in data_rows]

        meta = {}
        attribute_columns = []
        used_ids = set()
        parameters = []
        for index, header in enumerate(headers):
            key = _header_key(header)
            if not header:
                continue
            if key in ID_HEADERS:
                meta.setdefault("id", index)
            elif key in NAME_HEADERS:
                meta.setdefault("name", index)
            elif key in PRICE_HEADERS or ("价格" in header and "price" not in meta):
                meta.setdefault("price", index)
            elif key in TAG_HEADERS:
                meta.setdefault("tags", index)
            elif key in YEAR_HEADERS:
                meta.setdefault("year", index)
            elif key in SUPPLIER_HEADERS:
                meta.setdefault("supplier", index)
            elif key in POSITION_HEADERS:
                meta.setdefault("positioning", index)
            else:
                parameter_id = field_ids[index] if field_ids else _parameter_id(header, len(attribute_columns) + 1, used_ids)
                values = [row[index] if index < len(row) else "" for row in padded]
                parameter = _infer_parameter(header, parameter_id, values, tokens, len(attribute_columns) + 1)
                parameters.append(parameter)
                attribute_columns.append((index, parameter))

        if not parameters:
            raise ValueError("没有识别到可作为成品属性的列。")
        warnings = ["空单元格始终视为缺失；本次额外缺失标识：%s" % ("、".join(tokens) if tokens else "无")]
        if "id" not in meta:
            warnings.append("未找到成品/样本编号列，系统已生成 HIST-001 形式的编号。")
        if "name" not in meta:
            warnings.append("未找到成品/样本名称列，系统已生成“历史成品001”形式的名称。")
        if "price" not in meta:
            warnings.append("未识别到价格列；可稍后在历史协议模块逐条补充。")

        tag_names = []
        if "tags" in meta:
            for row in padded:
                raw_tags = row[meta["tags"]] if meta["tags"] < len(row) else ""
                if _is_missing(raw_tags, tokens):
                    continue
                for tag in split_tags(raw_tags):
                    if tag not in tag_names:
                        tag_names.append(tag)
        tags = [{
            "tag_id": "TAG%03d" % (index + 1), "tag_name": tag, "tag_group": "历史导入",
            "weight": 1.0, "derivation_mode": "manual", "description": "来自历史成品表",
            "enabled": 1,
        } for index, tag in enumerate(tag_names)]
        tag_by_name = dict((item["tag_name"], item["tag_id"]) for item in tags)

        agreements = []
        seen_ids = set()
        for row_index, row in enumerate(padded, 1):
            agreement_id = _clean(row[meta["id"]]) if "id" in meta and meta["id"] < len(row) and not _is_missing(row[meta["id"]], tokens) else ""
            if not agreement_id:
                agreement_id = "HIST-%03d" % row_index
            base_id = agreement_id
            suffix = 2
            while agreement_id in seen_ids:
                agreement_id = "%s-%d" % (base_id, suffix)
                suffix += 1
            seen_ids.add(agreement_id)
            agreement_name = _clean(row[meta["name"]]) if "name" in meta and meta["name"] < len(row) and not _is_missing(row[meta["name"]], tokens) else ""
            if not agreement_name:
                agreement_name = "历史成品%03d" % row_index
            item = {
                "agreement_id": agreement_id, "product_code": code, "agreement_name": agreement_name,
                "positioning": _clean(row[meta["positioning"]]) if "positioning" in meta else "",
                "agreement_source": "historical", "source_year": None, "supplier_type": "",
                "historical_price_wan": None, "tags": [], "params": {}, "enabled": 1,
            }
            if "year" in meta and not _is_missing(row[meta["year"]], tokens):
                number = _numeric_token(row[meta["year"]])
                item["source_year"] = int(number) if number is not None else None
            if "supplier" in meta and not _is_missing(row[meta["supplier"]], tokens):
                item["supplier_type"] = _clean(row[meta["supplier"]])
            if "price" in meta:
                item["historical_price_wan"] = _price_value(row[meta["price"]], headers[meta["price"]], tokens, warnings)
            if "tags" in meta:
                raw_tags = row[meta["tags"]]
                if not _is_missing(raw_tags, tokens):
                    item["tags"] = [tag_by_name[tag] for tag in split_tags(raw_tags) if tag in tag_by_name]
            for column_index, parameter in attribute_columns:
                value = _typed_value(row[column_index] if column_index < len(row) else "", parameter, tokens)
                if value is not None:
                    item["params"][parameter["parameter_id"]] = value
            agreements.append(item)

        ambiguous = [item for item in parameters if item.get("inference", {}).get("confidence") == "needs_confirmation"]
        if ambiguous:
            warnings.append("%d个字段需要人工确认，主要是含义不明确的0/1字段或整列缺失字段。" % len(ambiguous))
        inference_rows = []
        for parameter in parameters:
            inference = parameter.get("inference") or {}
            inference_rows.append({
                "source_header": inference.get("source_header"), "parameter_id": parameter["parameter_id"],
                "label": parameter["label"], "value_type": parameter["value_type"],
                "search_type": parameter["search_type"], "required": parameter["required"],
                "observed_count": inference.get("observed_count"), "missing_count": inference.get("missing_count"),
                "unique_count": inference.get("unique_count"), "confidence": inference.get("confidence"),
                "note": inference.get("note"),
            })
        data = {
            "products": [{"product_code": code, "product_name": name, "product_description": "由历史成品宽表自动建立的维护草稿", "enabled": 1}],
            "parameters": parameters,
            "tags": tags,
            "tag_rules": [],
            "couplings": [],
            "constraints": [],
            "agreements": agreements,
            "onboarding": {
                "source_filename": filename, "missing_tokens": tokens, "inference_rows": inference_rows,
                "warnings": warnings, "header_rows": table["header_rows"], "field_ids": field_ids, "input_diagnostics": table.get("diagnostics"),
            },
        }
        return {
            "filename": filename, "product_code": code, "product_name": name,
            "row_count": len(agreements), "attribute_count": len(parameters),
            "required_attribute_count": sum(1 for item in parameters if item.get("required")),
            "optional_attribute_count": sum(1 for item in parameters if not item.get("required")),
            "needs_confirmation_count": len(ambiguous), "price_column_detected": "price" in meta,
            "generated_id_count": len(agreements) if "id" not in meta else 0,
            "missing_tokens": tokens, "warnings": warnings, "header_rows": table["header_rows"], "field_ids": field_ids, "input_diagnostics": table.get("diagnostics"), "inferred_parameters": inference_rows,
            "data": data,
        }
