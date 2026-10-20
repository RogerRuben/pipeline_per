# -*- coding: utf-8 -*-
"""Product-table selection, safe header normalization and cell diagnostics."""
from __future__ import print_function

import re
import unicodedata

from .wide_import import read_csv_table
from .xlsx_utils import column_name, read_workbook_details


FIELD_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")
CJK = re.compile(r"[\u3400-\u9fff]")
_INVISIBLE = dict.fromkeys(map(ord, "\ufeff\u200b\u200c\u200d\u200e\u200f\u2060"))


def clean_field_id(value):
    return "".join(ch for ch in unicodedata.normalize("NFKC", str(value if value is not None else ""))
                   if unicodedata.category(ch) != "Cf").strip()


def _label(value):
    return str(value if value is not None else "").translate(_INVISIBLE).strip()


class TableFormatError(ValueError):
    def __init__(self, message, details):
        ValueError.__init__(self, message)
        self.details = details


def _prepared(sheet):
    formula_rows = {entry["row"] for entry in sheet.get("formulas", {}).values()}
    source = [(row, number) for row, number in zip(sheet["rows"], sheet["row_numbers"])
              if any(str(value if value is not None else "").strip() for value in row) or number in formula_rows]
    columns = {i for row, _ in source for i, value in enumerate(row) if str(value if value is not None else "").strip()}
    columns.update(entry["column"] for entry in sheet.get("formulas", {}).values())
    columns = sorted(columns)
    rows = [[row[i] if i < len(row) else "" for i in columns] for row, _ in source]
    ignored = set(sheet.get("seen_columns", [])) - set(columns)
    return dict(sheet, rows=rows, row_numbers=[n for _, n in source], columns=columns,
                ignored_empty_columns=[column_name(i) for i in sorted(ignored)])


def _hint(sheet):
    if len(sheet["rows"]) < 2:
        return False
    labels = [_label(x) for x in sheet["rows"][0]]
    codes = [clean_field_id(x) for x in sheet["rows"][1]]
    identity = any(re.match(r"^(product|agreement|sample|scheme)_(id|name)$", code, re.I) for code in codes)
    price = any("价格" in label and i < len(codes) and FIELD_ID.fullmatch(codes[i]) for i, label in enumerate(labels))
    return any(CJK.search(label) for label in labels) and (identity or price)


def _valid_dual(sheet):
    if len(sheet["rows"]) < 3 or not _hint(sheet):
        return False
    labels, codes = [_label(x) for x in sheet["rows"][0]], [clean_field_id(x) for x in sheet["rows"][1]]
    return bool(labels) and all(CJK.search(x) for x in labels) and all(FIELD_ID.fullmatch(x) for x in codes) and len(set(x.lower() for x in codes)) == len(codes)


def _details(filename, sheet):
    header_cells = []
    for row in range(min(2, len(sheet["rows"]))):
        for index, value in enumerate(sheet["rows"][row]):
            original = str(value if value is not None else "")
            parsed = _label(value) if row == 0 else clean_field_id(value)
            header_cells.append({"cell": "%s%d" % (column_name(sheet["columns"][index]), sheet["row_numbers"][row]),
                                 "raw": original, "parsed": parsed, "normalized": original != parsed})
    return {"filename": str(filename), "sheet_name": sheet["name"], "header_cells": header_cells,
            "header_row_numbers": sheet["row_numbers"][:2], "ignored_empty_columns": sheet["ignored_empty_columns"],
            "delimiter": sheet.get("delimiter"), "formulas": sheet.get("formulas", {})}


def product_table_rows(filename, raw, require_dual_header=False):
    if str(filename).lower().endswith(".xlsx"):
        visible = [_prepared(s) for s in read_workbook_details(raw) if s["state"] == "visible"]
        named = [s for s in visible if _label(s["name"]) == "成品数据"]
        valid = [s for s in visible if _valid_dual(s)]
        hinted = [s for s in visible if _hint(s)]
        candidates = named or valid or hinted or [s for s in visible if s["rows"]][:1]
        if len(candidates) != 1:
            names = [s["name"] for s in (candidates or visible)]
            raise TableFormatError("无法唯一确定成品数据工作表：%s。请将要使用的可见工作表命名为“成品数据”。" % "、".join(names),
                                   {"filename": str(filename), "sheets": names, "stage": "sheet_selection"})
        sheet = candidates[0]
    elif str(filename).lower().endswith(".csv") or not filename:
        parsed = read_csv_table(raw)
        sheet = _prepared(dict(parsed, name="CSV", state="visible", formulas={},
                               seen_columns=sorted({i for row in parsed["rows"] for i in range(len(row))})))
    else:
        raise ValueError("仅支持.csv和.xlsx成品表。")
    details = _details(filename, sheet)
    rows = sheet["rows"]
    def fail(message, cell=None):
        context = dict(details, error=message)
        if cell:
            context["error_cell"] = cell
        raise TableFormatError(message, context)
    if len(rows) < 2:
        fail("工作表“%s”至少需要表头和一条数据。" % sheet["name"])
    labels = [_label(value) for value in rows[0]]
    codes = [clean_field_id(value) for value in rows[1]]
    dual = require_dual_header or _hint(sheet)
    ids = None
    if dual:
        used = {}
        for i, code in enumerate(codes):
            coordinate = "%s%d" % (column_name(sheet["columns"][i]), sheet["row_numbers"][1])
            original = str(rows[1][i] if rows[1][i] is not None else "")
            if not FIELD_ID.fullmatch(code):
                formula = sheet.get("formulas", {}).get(coordinate)
                reason = "英文代号为空" if not code else "英文代号须以字母开头，仅含英文字母、数字、下划线"
                if formula and not formula["cached"]:
                    reason = "表头公式没有保存计算结果，请先计算并保存，或粘贴为值"
                invalid = ["U+%04X" % ord(ch) for ch in code if not (ch.isascii() and (ch.isalnum() or ch == "_"))]
                suffix = "；字符编码：" + ", ".join(invalid) if invalid else ""
                fail("工作表“%s”的%s：%s；原值=%r；对应中文名=%r%s。" % (sheet["name"], coordinate, reason, original, labels[i], suffix), coordinate)
            if code.lower() in used:
                fail("工作表“%s”的%s与%s英文代号重复（含清理不可见字符后的重复）：%r。" %
                     (sheet["name"], coordinate, used[code.lower()], code), coordinate)
            used[code.lower()] = coordinate
            if not CJK.search(labels[i]):
                location = "%s%d" % (column_name(sheet["columns"][i]), sheet["row_numbers"][0])
                fail("工作表“%s”的%s缺少中文名称；原值=%r；该列英文代号=%r。" % (sheet["name"], location, rows[0][i], code), location)
        ids = codes
    body = rows[2:] if dual else rows[1:]
    if not body:
        fail("工作表“%s”没有成品记录。" % sheet["name"])
    details["status"] = "PASS"
    details["header_rows"] = 2 if dual else 1
    return {"labels": labels, "field_ids": ids, "rows": body, "header_rows": 2 if dual else 1,
            "sheet_name": sheet["name"], "diagnostics": details}
