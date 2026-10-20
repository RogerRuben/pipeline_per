# -*- coding: utf-8 -*-
"""Regression cases for WinError 31 and actual Excel/CSV header edge cases."""
from __future__ import print_function

import csv
import io
import json
import multiprocessing as mp
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.product_table import product_table_rows, TableFormatError
from app.xlsx_utils import write_workbook_bytes, read_workbook_bytes

NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
LABELS = ["成品编号", "成品名称", "价格(万元)", "载荷(N)"]
IDS = ["product_id", "product_name", "price_wan", "Load_N"]
BODY = [["X01", "成品甲", 5.5, 1000], ["X02", "成品乙", 6., 1400]]


def workbook(rows=None, name="成品数据"):
    return write_workbook_bytes([(name, rows or [LABELS, IDS] + BODY)])


def amend(raw, transformer):
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
        for member in source.infolist():
            target.writestr(member, transformer(member.filename, source.read(member.filename)))
    return output.getvalue()


class BrokenTerminal(object):
    def __init__(self, stream, fail_write=False):
        self.stream, self.fail_write = stream, fail_write
        self.probes = 0
    def __getattr__(self, name):
        return getattr(self.stream, name)
    def isatty(self):
        self.probes += 1
        raise PermissionError(13, "A device attached to the system is not functioning.", None, 31)
    def fileno(self):
        self.probes += 1
        raise PermissionError(13, "A device attached to the system is not functioning.", None, 31)
    @property
    def closed(self):
        self.probes += 1
        raise PermissionError(13, "A device attached to the system is not functioning.", None, 31)
    def write(self, value):
        if self.fail_write:
            raise PermissionError(13, "A device attached to the system is not functioning.", None, 31)
        return self.stream.write(value)
    def flush(self):
        if self.fail_write:
            raise PermissionError(13, "A device attached to the system is not functioning.", None, 31)
        self.stream.flush()


def console_worker(queue, directory):
    from app.console_compat import enable_plain_console
    terminal = BrokenTerminal(sys.stderr)
    sys.stderr = terminal
    enable_plain_console(directory)
    import optuna
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    study = optuna.create_study()
    study.optimize(lambda trial: trial.suggest_float("x", 0, 1) ** 2, n_trials=2)
    queue.put({"trials": len(study.trials), "probes": terminal.probes, "plain": os.environ["NO_COLOR"]})


class PortableCompatibilityTests(unittest.TestCase):
    def table(self, raw):
        return product_table_rows("products.xlsx", raw, require_dual_header=True)

    def test_styled_empty_cells_do_not_change_the_schema(self):
        raw = amend(workbook(), lambda path, data: data.replace(b"</row>", b'<c r="XFD1" s="0"/></row>', 1)
                    if path == "xl/worksheets/sheet1.xml" else data)
        result = self.table(raw)
        self.assertEqual(result["field_ids"], IDS)
        self.assertEqual(len(result["rows"][0]), 4)
        self.assertIn("XFD", result["diagnostics"]["ignored_empty_columns"])

    def test_empty_middle_and_trailing_columns_are_ignored_only_if_all_rows_empty(self):
        rows = [[row[0], ""] + list(row[1:]) + [None, ""] for row in [LABELS, IDS] + BODY]
        result = self.table(workbook(rows))
        self.assertEqual(result["field_ids"], IDS)
        self.assertEqual(result["rows"][0][3], "1000")
        self.assertIn("B", result["diagnostics"]["ignored_empty_columns"])

    def test_real_unlabelled_data_column_is_not_silently_dropped(self):
        rows = [LABELS + [""], IDS + [""], BODY[0] + [123], BODY[1] + [456]]
        with self.assertRaises(TableFormatError) as caught:
            self.table(workbook(rows))
        self.assertEqual(caught.exception.details["error_cell"], "E2")
        self.assertIn("原值", str(caught.exception))

    def test_named_product_sheet_is_selected_after_expert_sheet(self):
        raw = write_workbook_bytes([("专家模型权重", [["模型族", "中文名", "份额"], ["linear", "线性", .5]]),
                                    ("成品数据", [LABELS, IDS] + BODY)])
        self.assertEqual(self.table(raw)["field_ids"], IDS)
        self.assertEqual(self.table(raw)["sheet_name"], "成品数据")

    def test_hidden_data_sheet_is_not_selected(self):
        raw = write_workbook_bytes([("成品数据", [LABELS, IDS] + BODY), ("实际数据", [LABELS, IDS] + BODY)])
        def hide(path, data):
            if path == "xl/workbook.xml":
                root = ET.fromstring(data)
                root.find("{%s}sheets/{%s}sheet" % (NS, NS)).set("state", "hidden")
                return ET.tostring(root, encoding="utf-8")
            return data
        self.assertEqual(self.table(amend(raw, hide))["sheet_name"], "实际数据")

    def test_ambiguous_product_sheets_are_rejected(self):
        raw = write_workbook_bytes([("数据甲", [LABELS, IDS] + BODY), ("数据乙", [LABELS, IDS] + BODY)])
        with self.assertRaises(TableFormatError) as caught:
            self.table(raw)
        self.assertIn("数据甲", str(caught.exception))
        self.assertIn("数据乙", str(caught.exception))

    def test_named_empty_sheet_does_not_fall_back_to_other_product(self):
        raw = write_workbook_bytes([("成品数据", []), ("其他数据", [LABELS, IDS] + BODY)])
        with self.assertRaises(TableFormatError) as caught:
            self.table(raw)
        self.assertIn("成品数据", str(caught.exception))

    def test_invisible_and_fullwidth_header_characters_are_normalized_without_lowercasing(self):
        rows = [LABELS, IDS[:3] + ["Ｌｏａｄ＿Ｎ\u200b\ufeff"]] + BODY
        result = self.table(workbook(rows))
        self.assertEqual(result["field_ids"], IDS)
        self.assertTrue(next(cell for cell in result["diagnostics"]["header_cells"] if cell["cell"] == "D2")["normalized"])

    def test_normalization_collisions_are_rejected(self):
        rows = [LABELS, IDS[:3] + ["price_wan\u200b"]] + BODY
        with self.assertRaises(TableFormatError) as caught:
            self.table(workbook(rows))
        self.assertIn("C2", str(caught.exception)); self.assertIn("D2", str(caught.exception))

    def test_invalid_id_reports_sheet_cell_and_character(self):
        rows = [LABELS, IDS[:3] + ["Load-N"]] + BODY
        with self.assertRaises(TableFormatError) as caught:
            self.table(workbook(rows))
        self.assertEqual(caught.exception.details["sheet_name"], "成品数据")
        self.assertEqual(caught.exception.details["error_cell"], "D2")
        self.assertIn("U+002D", str(caught.exception))

    def test_original_row_coordinates_survive_blank_rows(self):
        rows = [[None] * 4, LABELS, IDS[:3] + ["bad-id"]] + BODY
        with self.assertRaises(TableFormatError) as caught:
            self.table(workbook(rows))
        self.assertEqual(caught.exception.details["error_cell"], "D3")

    def test_header_formulas_require_a_cached_value(self):
        def formula(path, data, cache):
            if path != "xl/worksheets/sheet1.xml":
                return data
            root = ET.fromstring(data)
            cell = next(c for c in root.iter("{%s}c" % NS) if c.get("r") == "D2")
            cell.clear(); cell.set("r", "D2"); cell.set("t", "str")
            ET.SubElement(cell, "{%s}f" % NS).text = '"Load_N"'
            if cache:
                ET.SubElement(cell, "{%s}v" % NS).text = "Load_N"
            return ET.tostring(root, encoding="utf-8")
        self.assertEqual(self.table(amend(workbook(), lambda p, d: formula(p, d, True)))["field_ids"], IDS)
        with self.assertRaises(TableFormatError) as caught:
            self.table(amend(workbook(), lambda p, d: formula(p, d, False)))
        self.assertIn("D2", str(caught.exception)); self.assertIn("公式", str(caught.exception))

    def test_strict_ooxml_namespace_and_relationships(self):
        raw = amend(workbook(), lambda p, d: d.replace(NS.encode(), b"http://purl.oclc.org/ooxml/spreadsheetml/main")
                    .replace(b"http://schemas.openxmlformats.org/officeDocument/2006/relationships", b"http://purl.oclc.org/ooxml/officeDocument/relationships")
                    if p.endswith(".xml") else d)
        self.assertEqual(self.table(raw)["field_ids"], IDS)
        self.assertEqual(len(read_workbook_bytes(raw)["成品数据"]), 4)

    def test_csv_delimiters_and_utf16_export(self):
        for delimiter in (",", ";", "\t"):
            text = io.StringIO(); csv.writer(text, delimiter=delimiter).writerows([LABELS, IDS] + BODY)
            for encoding in ("utf-8-sig", "utf-16", "gb18030"):
                raw = text.getvalue().encode(encoding)
                result = product_table_rows("data.csv", raw, True)
                self.assertEqual(result["field_ids"], IDS)
                self.assertEqual(result["diagnostics"]["delimiter"], delimiter)
        text = "sep=;\n" + ";".join(LABELS) + "\n" + ";".join(IDS) + "\nX1;测试;5;1000\n"
        result = product_table_rows("data.csv", text.encode("utf-8"), True)
        self.assertEqual(result["diagnostics"]["header_row_numbers"], [2, 3])

    def test_normal_legacy_ascii_record_is_not_consumed_as_header(self):
        raw = "成品编号,成品名称,材料,工艺\nS001,Basic,Steel,Milled\nS002,Light,Aluminium,Cast\n".encode("utf-8")
        result = product_table_rows("legacy.csv", raw)
        self.assertEqual(result["header_rows"], 1)
        self.assertEqual(len(result["rows"]), 2)

    def test_broken_terminal_probes_do_not_break_optuna_import(self):
        program = '''
import sys
sys.path.insert(0, sys.argv[1])
from tests.portable_compatibility_test import BrokenTerminal
from app.console_compat import enable_plain_console
original = sys.stderr
broken = BrokenTerminal(original)
sys.stderr = broken
enable_plain_console()
import optuna
optuna.logging.set_verbosity(optuna.logging.WARNING)
study = optuna.create_study()
study.optimize(lambda t: t.suggest_float("x", 0, 1), n_trials=2)
assert broken.probes == 0 and len(study.trials) == 2
print("PASS")
'''
        result = subprocess.run([sys.executable, "-X", "utf8", "-c", program, str(ROOT)], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("PASS", result.stdout)

    def test_failed_terminal_writes_are_logged_and_filesystem_errors_still_raise(self):
        from app.console_compat import PlainOutput
        (ROOT / "outputs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="console_compat_", dir=str(ROOT / "outputs")) as directory:
            self.assertIn((ROOT / "outputs").resolve(), Path(directory).resolve().parents)
            stream = PlainOutput(BrokenTerminal(io.StringIO(), True), directory, "stderr")
            stream.write("训练仍在进行\n"); stream.flush()
            saved = next(Path(directory).glob("console_fallback_*.log")).read_text(encoding="utf-8")
            self.assertIn("WinError 31", saved); self.assertIn("训练仍在进行", saved)
            blocker = Path(directory) / "not_a_directory"; blocker.write_text("blocked")
            with self.assertRaises(OSError):
                PlainOutput(None, blocker, "stderr").write("must not swallow file errors")

    def test_spawn_worker_uses_same_console_protection(self):
        context = mp.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=console_worker, args=(queue, str(ROOT / "logs")))
        process.start()
        try:
            result = queue.get(timeout=30)
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(result, {"trials": 2, "probes": 0, "plain": "1"})
        finally:
            if process.is_alive():
                process.terminate(); process.join(timeout=5)
            queue.close(); queue.join_thread()


if __name__ == "__main__":
    mp.freeze_support()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PortableCompatibilityTests))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "portable_compatibility_test_report.json").write_text(json.dumps(
        {"status": "PASS" if result.wasSuccessful() else "FAIL", "tests": result.testsRun,
         "failures": len(result.failures), "errors": len(result.errors)}, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
