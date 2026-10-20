# -*- coding: utf-8 -*-
"""Small dependency-free XLSX reader/writer for DataMaster workbooks.

The implementation intentionally supports the simple tabular subset used by
this project, so Windows 7 deployments do not need openpyxl or pandas.
"""
from __future__ import print_function

import io
import posixpath
import zipfile
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PKGREL = "http://schemas.openxmlformats.org/package/2006/relationships"


def column_name(index):
    index = int(index) + 1
    result = ""
    while index:
        index, rem = divmod(index - 1, 26)
        result = chr(65 + rem) + result
    return result


def column_index(ref):
    letters = "".join(ch for ch in str(ref) if ch.isalpha()).upper()
    value = 0
    for ch in letters:
        value = value * 26 + ord(ch) - 64
    return max(value - 1, 0)


def read_workbook_details(data):
    """Values, visibility, physical coordinates and uncalculated formulas.

    Styled empty cells never extend data rows. Formula results are not invented.
    """
    result = []
    with zipfile.ZipFile(io.BytesIO(data), "r") as zf:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        main = workbook.tag.split("}")[0].lstrip("{")
        ns = {"m": main}
        shared = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root.findall("m:si", ns):
                shared.append("".join((node.text or "") for node in si.findall(".//m:t", ns)))
        rels = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        relmap = dict((rel.attrib["Id"], rel.attrib["Target"]) for rel in rels)
        for sheet in workbook.findall("m:sheets/m:sheet", ns):
            name = sheet.attrib.get("name", "Sheet")
            rid = next((value for key, value in sheet.attrib.items() if key.endswith("}id")), None)
            target = relmap[rid]
            target = posixpath.normpath(target.lstrip("/") if target.startswith(("/", "xl/")) else posixpath.join("xl", target))
            if target == ".." or target.startswith("../"):
                raise ValueError("工作表%s的文件关系越出工作簿。" % name)
            root = ET.fromstring(zf.read(target))
            rows, row_numbers, formulas, seen_columns = [], [], {}, set()
            for sequence, row in enumerate(root.findall(".//m:sheetData/m:row", ns), 1):
                row_number = int(row.attrib.get("r", sequence))
                values = {}
                next_column = 0
                for cell in row.findall("m:c", ns):
                    idx = column_index(cell.attrib["r"]) if cell.attrib.get("r") else next_column
                    next_column = idx + 1
                    seen_columns.add(idx)
                    reference = "%s%d" % (column_name(idx), row_number)
                    cell_type = cell.attrib.get("t")
                    value_node = cell.find("m:v", ns)
                    inline = cell.find("m:is", ns)
                    formula = cell.find("m:f", ns)
                    if formula is not None:
                        formulas[reference] = {"formula": formula.text or "", "cached": value_node is not None and value_node.text is not None,
                                               "column": idx, "row": row_number}
                    if cell_type == "s" and value_node is not None:
                        try:
                            value = shared[int(value_node.text)]
                        except (ValueError, TypeError, IndexError):
                            raise ValueError("工作表%s的%s引用了无效共享字符串。" % (name, reference))
                    elif cell_type == "inlineStr" and inline is not None:
                        value = "".join((node.text or "") for node in inline.findall(".//m:t", ns))
                    elif cell_type == "b" and value_node is not None:
                        value = "1" if value_node.text == "1" else "0"
                    else:
                        value = value_node.text if value_node is not None else ""
                    if str(value if value is not None else "").strip() or formula is not None:
                        values[idx] = value
                if values:
                    width = max(values) + 1
                    rows.append([values.get(i, "") for i in range(width)])
                    row_numbers.append(row_number)
            result.append({"name": name, "state": sheet.attrib.get("state", "visible"), "rows": rows,
                           "row_numbers": row_numbers, "formulas": formulas, "seen_columns": sorted(seen_columns)})
    return result


def read_workbook_bytes(data):
    return {sheet["name"]: sheet["rows"] for sheet in read_workbook_details(data)}


def _cell_xml(row_number, col_number, value, style=0):
    ref = "%s%d" % (column_name(col_number), row_number)
    if value is None:
        return '<c r="%s" s="%d"/>' % (ref, style)
    if isinstance(value, bool):
        return '<c r="%s" s="%d" t="b"><v>%d</v></c>' % (ref, style, 1 if value else 0)
    if isinstance(value, (int, float)):
        return '<c r="%s" s="%d"><v>%s</v></c>' % (ref, style, value)
    text = escape(str(value))
    preserve = ' xml:space="preserve"' if text != text.strip() else ""
    return '<c r="%s" s="%d" t="inlineStr"><is><t%s>%s</t></is></c>' % (ref, style, preserve, text)


def _validation_xml(items):
    items = list(items or [])
    if not items:
        return ""
    chunks = ['<dataValidations count="%d">' % len(items)]
    for item in items:
        attrs = {
            "type": item.get("type", "list"),
            "allowBlank": "1" if item.get("allow_blank", True) else "0",
            "showInputMessage": "1",
            "showErrorMessage": "1",
            "errorStyle": item.get("error_style", "stop"),
            "sqref": item.get("sqref", "A2:A1000"),
        }
        if item.get("prompt_title"):
            attrs["promptTitle"] = item.get("prompt_title")
        if item.get("prompt"):
            attrs["prompt"] = item.get("prompt")
        if item.get("error_title"):
            attrs["errorTitle"] = item.get("error_title")
        if item.get("error"):
            attrs["error"] = item.get("error")
        attr_text = " ".join('%s="%s"' % (key, escape(str(value), {'"':'&quot;'})) for key, value in attrs.items())
        formula = item.get("formula1")
        if not formula and item.get("values") is not None:
            formula = '"%s"' % ",".join(str(value) for value in item.get("values") or [])
        chunks.append('<dataValidation %s><formula1>%s</formula1></dataValidation>' % (attr_text, escape(str(formula or ""))))
    chunks.append('</dataValidations>')
    return "".join(chunks)


def write_workbook_bytes(sheets, validations=None, defined_names=None):
    """Create a compact XLSX from ``[(sheet_name, rows), ...]``.

    ``validations`` maps a sheet name to list-validation definitions.  The
    dependency-free writer keeps DataMaster usable on Windows 7 without
    openpyxl while still giving non-technical maintainers Excel drop-downs.
    """
    sheets = list(sheets)
    validations = validations or {}
    defined_names = defined_names or {}
    content_types = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">',
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>',
        '<Default Extension="xml" ContentType="application/xml"/>',
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i in range(len(sheets)):
        content_types.append('<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1))
    content_types.append('</Types>')

    workbook_sheets = []
    workbook_rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>', '<Relationships xmlns="%s">' % _PKGREL]
    for i, (name, rows) in enumerate(sheets, 1):
        workbook_sheets.append('<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (escape(str(name)), i, i))
        workbook_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i, i))
    workbook_rels.append('<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (len(sheets) + 1))
    workbook_rels.append('</Relationships>')

    defined_xml = ""
    if defined_names:
        defined_xml = "<definedNames>%s</definedNames>" % "".join(
            '<definedName name="%s">%s</definedName>' % (escape(str(name)), escape(str(formula)))
            for name, formula in defined_names.items()
        )
    workbook_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="%s" xmlns:r="%s"><sheets>%s</sheets>%s</workbook>') % (_MAIN, _REL, ''.join(workbook_sheets), defined_xml)
    root_rels = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="%s"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>') % _PKGREL
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="0"/><fonts count="2"><font><sz val="11"/><name val="Microsoft YaHei"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Microsoft YaHei"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF1F4E78"/><bgColor indexed="64"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1" applyAlignment="1"><alignment horizontal="center" vertical="center" wrapText="1"/></xf></cellXfs>
<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles></styleSheet>'''

    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", ''.join(content_types))
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("xl/workbook.xml", workbook_xml)
        zf.writestr("xl/_rels/workbook.xml.rels", ''.join(workbook_rels))
        zf.writestr("xl/styles.xml", styles)
        for i, (sheet_name, rows) in enumerate(sheets, 1):
            max_cols = max([len(row) for row in rows] or [1])
            cols = ''.join('<col min="%d" max="%d" width="%s" customWidth="1"/>' % (c + 1, c + 1, 22 if c else 18) for c in range(max_cols))
            row_xml = []
            for r_index, row in enumerate(rows, 1):
                cells = ''.join(_cell_xml(r_index, c, value, 1 if r_index == 1 else 0) for c, value in enumerate(row))
                row_xml.append('<row r="%d"%s>%s</row>' % (r_index, ' ht="26" customHeight="1"' if r_index == 1 else '', cells))
            validation_xml = _validation_xml(validations.get(sheet_name))
            sheet_xml = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="%s"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                '<cols>%s</cols><sheetData>%s</sheetData><autoFilter ref="A1:%s%d"/>%s</worksheet>') % (_MAIN, cols, ''.join(row_xml), column_name(max_cols - 1), max(len(rows), 1), validation_xml)
            zf.writestr("xl/worksheets/sheet%d.xml" % i, sheet_xml)
    return output.getvalue()
