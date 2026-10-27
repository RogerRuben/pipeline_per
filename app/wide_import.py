# -*- coding: utf-8 -*-
"""Protocol wide-table parser using only the Python standard library.

Supports UTF-8/GB18030 CSV and ordinary .xlsx workbooks. The first visible
worksheet is interpreted as a protocol wide table. Product/effectiveness
attributes are required; price-only inputs are handled by the price bundle's
own input contract and missing-value policies.
"""
from __future__ import print_function

import csv
import io
import json
import re
import zipfile
from xml.etree import ElementTree as ET


META_ALIASES = {
    "协议编号": "agreement_id", "技术协议编号": "agreement_id", "agreement_id": "agreement_id",
    "成品代号": "product_code", "产品代号": "product_code", "product_code": "product_code",
    "协议名称": "agreement_name", "技术协议名称": "agreement_name", "agreement_name": "agreement_name",
    "方案定位": "positioning", "协议定位": "positioning", "positioning": "positioning",
    "协议来源": "agreement_source", "来源": "agreement_source", "agreement_source": "agreement_source",
    "来源年份": "source_year", "年份": "source_year", "source_year": "source_year",
    "供应方类型": "supplier_type", "供应商类型": "supplier_type", "supplier_type": "supplier_type",
    "历史价格(万元)": "historical_price_wan", "历史价格（万元）": "historical_price_wan",
    "历史价格": "historical_price_wan", "historical_price_wan": "historical_price_wan",
    "标签": "tags", "标签编号": "tags", "tags": "tags",
    "是否启用": "enabled", "enabled": "enabled",
}


def normalize_header(value):
    return re.sub(r"\s+", "", str(value or "").strip()).lower()


def _column_index(ref):
    letters = "".join(ch for ch in str(ref) if ch.isalpha()).upper()
    value = 0
    for ch in letters:
        value = value * 26 + ord(ch) - 64
    return max(value - 1, 0)


def _decode_text(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            pass
    raise ValueError("CSV编码无法识别，请保存为UTF-8或GB18030。")


def read_csv_table(data):
    text = _decode_text(data)
    lines = text.splitlines(True)
    offset, delimiter = 0, None
    if lines and re.fullmatch(r"sep=([,;\t|])", lines[0].strip(), re.I):
        delimiter = lines[0].strip()[4:]
        text, offset = "".join(lines[1:]), 1
    if delimiter is None:
        try:
            delimiter = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|").delimiter
        except csv.Error:
            delimiter = ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    rows, numbers, previous = [], [], offset
    for row in reader:
        rows.append(list(row))
        numbers.append(previous + 1)
        previous = reader.line_num + offset
    return {"rows": rows, "row_numbers": numbers, "delimiter": delimiter}


def read_csv_bytes(data):
    return read_csv_table(data)["rows"]


def read_xlsx_bytes(data):
    from .xlsx_utils import read_workbook_details
    sheets = read_workbook_details(data)
    return next((sheet["rows"] for sheet in sheets if sheet["state"] == "visible"), [])


def read_table_bytes(filename, data):
    lower = str(filename or "").lower()
    if lower.endswith(".xlsx"):
        return read_xlsx_bytes(data)
    if lower.endswith(".csv") or not lower:
        return read_csv_bytes(data)
    raise ValueError("仅支持.csv和.xlsx宽表。")


def parse_bool(value):
    text = str(value or "").strip().lower()
    if text in ("1", "true", "yes", "y", "有", "是", "启用", "具备"):
        return 1
    if text in ("0", "false", "no", "n", "无", "否", "停用", "不具备", ""):
        return 0
    raise ValueError("无法识别布尔值%s" % value)


def split_tags(value):
    if isinstance(value, (list, tuple)):
        return [str(x).strip() for x in value if str(x).strip()]
    return [x.strip() for x in re.split(r"[、,，;；|/]+", str(value or "")) if x.strip()]


def allowed_values(definition):
    raw = definition.get("allowed_values_json", definition.get("allowed_values"))
    if raw in (None, ""):
        return []
    if isinstance(raw, (list, tuple)):
        return list(raw)
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    return [x.strip() for x in re.split(r"[、,，;；|]+", str(raw)) if x.strip()]


class WideTableParser(object):
    def __init__(self, parameters, tags, product_code):
        self.parameters = list(parameters)
        self.product_code = product_code
        self.parameter_by_id = dict((p["parameter_id"], p) for p in self.parameters)
        self.parameter_header = {}
        for p in self.parameters:
            self.parameter_header[normalize_header(p["parameter_id"])] = p["parameter_id"]
            self.parameter_header[normalize_header(p.get("label"))] = p["parameter_id"]
            if p.get("unit"):
                self.parameter_header[normalize_header("%s(%s)" % (p.get("label"), p.get("unit")))] = p["parameter_id"]
                self.parameter_header[normalize_header("%s（%s）" % (p.get("label"), p.get("unit")))] = p["parameter_id"]
        self.tag_by_id = dict((t["tag_id"], t) for t in tags)
        self.tag_by_name = dict((t["tag_name"], t["tag_id"]) for t in tags)

    def template_csv(self):
        headers = ["协议编号", "成品代号", "协议名称", "方案定位", "协议来源", "来源年份", "供应方类型", "历史价格(万元)", "标签"]
        headers += [p["label"] + (("(%s)" % p["unit"]) if p.get("unit") else "") for p in self.parameters]
        sample = ["IMP-001", self.product_code, "导入示例协议", "请填写方案定位", "imported", "2026", "供应方A", "12.500", "高推力、位置反馈"]
        for p in self.parameters:
            if p["value_type"] == "boolean": sample.append("有")
            elif p["value_type"] == "ip_grade": sample.append("IP67")
            elif p["value_type"] == "enum":
                choices = allowed_values(p)
                sample.append(str(choices[0]) if choices else "")
            elif p["value_type"] == "text": sample.append("示例文本")
            else:
                lo, hi = p.get("min_value"), p.get("max_value")
                sample.append(str(round((float(lo) + float(hi)) / 2.0, 3)) if lo is not None and hi is not None else "")
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\r\n")
        writer.writerow(headers); writer.writerow(sample)
        return ("\ufeff" + out.getvalue()).encode("utf-8")

    def parse(self, filename, data):
        rows = read_table_bytes(filename, data)
        rows = [row for row in rows if any(str(v).strip() for v in row)]
        if not rows:
            raise ValueError("宽表为空。")
        headers = [str(v or "").strip() for v in rows[0]]
        mapping, unknown = {}, []
        for idx, header in enumerate(headers):
            key = normalize_header(header)
            meta = META_ALIASES.get(key) or META_ALIASES.get(header)
            if meta:
                mapping[idx] = ("meta", meta)
            elif key in self.parameter_header:
                mapping[idx] = ("parameter", self.parameter_header[key])
            elif header:
                unknown.append(header)
        required_meta = {"agreement_id", "agreement_name"}
        present_meta = set(value for kind, value in mapping.values() if kind == "meta")
        missing_headers = sorted(required_meta - present_meta)
        if missing_headers:
            raise ValueError("宽表缺少必需列: %s" % ", ".join(missing_headers))

        parsed, global_warnings = [], []
        if unknown:
            global_warnings.append("未识别列将忽略: %s" % "、".join(unknown))
        seen = set()
        for row_number, row in enumerate(rows[1:], 2):
            item = {"product_code": self.product_code, "agreement_source": "imported", "enabled": 1, "tags": [], "params": {}}
            errors, warnings = [], []
            for idx, (kind, key) in mapping.items():
                value = row[idx] if idx < len(row) else ""
                if kind == "meta":
                    if key == "tags":
                        tags = []
                        for token in split_tags(value):
                            if token in self.tag_by_id: tags.append(token)
                            elif token in self.tag_by_name: tags.append(self.tag_by_name[token])
                            else: warnings.append("未知标签：%s" % token)
                        item["tags"] = sorted(set(tags))
                    elif key in ("source_year", "enabled"):
                        if str(value).strip() != "":
                            try: item[key] = int(float(value)) if key == "source_year" else parse_bool(value)
                            except Exception as exc: errors.append("%s：%s" % (headers[idx], exc))
                    elif key == "historical_price_wan":
                        if str(value).strip() != "":
                            try: item[key] = float(value)
                            except Exception: errors.append("历史价格不是有效数值")
                    elif str(value).strip() != "": item[key] = str(value).strip()
                else:
                    definition = self.parameter_by_id[key]
                    if str(value).strip() == "":
                        continue
                    try:
                        if definition["value_type"] == "boolean": parsed_value = parse_bool(value)
                        elif definition["value_type"] == "ip_grade":
                            text = str(value).strip().upper().replace("IP", "")
                            parsed_value = float(text)
                        elif definition["value_type"] in ("enum", "text"):
                            parsed_value = str(value).strip()
                            choices = allowed_values(definition)
                            if definition["value_type"] == "enum" and choices:
                                normalized = dict((str(choice).strip().lower(), choice) for choice in choices)
                                lookup = parsed_value.lower()
                                if lookup not in normalized:
                                    raise ValueError("取值%s不在允许范围%s" % (parsed_value, "、".join(str(choice) for choice in choices)))
                                parsed_value = normalized[lookup]
                        else: parsed_value = float(value)
                        item["params"][key] = parsed_value
                        lo, hi = definition.get("min_value"), definition.get("max_value")
                        if definition["value_type"] not in ("boolean", "enum", "text") and lo is not None and parsed_value < float(lo):
                            warnings.append("%s低于当前模型范围下限%s，将触发域外提醒" % (definition.get("label", key), lo))
                        if definition["value_type"] not in ("boolean", "enum", "text") and hi is not None and parsed_value > float(hi):
                            warnings.append("%s高于当前模型范围上限%s，将触发域外提醒" % (definition.get("label", key), hi))
                    except Exception as exc:
                        errors.append("%s：%s" % (definition.get("label", key), exc))
            if not item.get("agreement_id"): errors.append("协议编号为空")
            if not item.get("agreement_name"): errors.append("协议名称为空")
            if item.get("product_code") and item.get("product_code") != self.product_code:
                errors.append("成品代号%s与当前模型成品%s不一致" % (item.get("product_code"), self.product_code))
            if item.get("agreement_id") in seen: errors.append("协议编号在文件内重复")
            seen.add(item.get("agreement_id"))
            # "required" (must have a value) and "model_bound" (used by a model)
            # are different concepts: a model-bound field may legitimately be
            # optional and fall back to a model missing-value policy.  Only
            # business-required fields gate wide-table import.
            missing_params = [p["parameter_id"] for p in self.parameters if p.get("required") and item["params"].get(p["parameter_id"]) in (None, "")]
            if missing_params:
                labels = [self.parameter_by_id[k]["label"] for k in missing_params]
                errors.append("缺少业务必填属性：%s" % "、".join(labels))
            parsed.append({"row_number": row_number, "item": item, "errors": errors, "warnings": warnings, "valid": not errors})
        return {
            "filename": filename,
            "row_count": len(parsed),
            "valid_count": sum(1 for x in parsed if x["valid"]),
            "invalid_count": sum(1 for x in parsed if not x["valid"]),
            "global_warnings": global_warnings,
            "rows": parsed,
        }
