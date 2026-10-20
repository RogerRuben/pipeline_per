# -*- coding: utf-8 -*-
"""Validate the actual ZIP in a new Unicode/space directory with no host Python."""
from __future__ import print_function

import argparse
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.xlsx_utils import write_workbook_bytes
import io
import csv
from xml.etree import ElementTree as ET


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(command, cwd, env, timeout=120):
    result = subprocess.run(command, cwd=str(cwd), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            timeout=timeout, encoding="utf-8", errors="replace")
    if result.returncode:
        raise AssertionError(result.stdout[-10000:])
    return result.stdout


def exercise(package, env, records, features, log_path, input_file=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(url):
        with opener.open(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    def post(url, payload):
        request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
        with opener.open(request, timeout=60) as response:
            return json.loads(response.read().decode("utf-8"))
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as handle:
        command = ["cmd.exe", "/d", "/c", "START.bat"] + ([str(input_file)] if input_file else [])
        command += ["--no-browser", "--price-port", "0", "--effect-port", "0", "--app-port", "0"]
        process = subprocess.Popen(command,
                                   cwd=str(package), env=env, stdout=handle, stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        runtime, output = None, None
        try:
            deadline = time.monotonic() + 240
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(log_path.read_text(encoding="utf-8"))
                latest_path = package / "state" / "latest.json"
                if latest_path.is_file():
                    latest = json.loads(latest_path.read_text(encoding="utf-8"))
                    if latest["input_sha256"] == sha(input_file or package / "input" / "products.xlsx"):
                        output = package / latest["output"]
                        if (output / "running.json").is_file():
                            runtime = json.loads((output / "running.json").read_text(encoding="utf-8"))
                            break
                time.sleep(.2)
            if runtime is None:
                raise AssertionError(log_path.read_text(encoding="utf-8")[-12000:])
            base = runtime["url"].rstrip("/")
            bootstrap = get(base + "/api/bootstrap")
            assert bootstrap["counts"]["historical"] == records
            assert len(bootstrap["parameters"]) == features
            assert bootstrap["integration"]["product_ready"]
            schema = get(base + "/api/price-workbench/schema")
            assert len(schema["fields"]) == features
            assert all(any("\u3400" <= char <= "\u9fff" for char in field["field_label"]) for field in schema["fields"])
            payload = {"session_id": "portable-acceptance", "source_mode": "historical", "selected_tags": [], "indicator_filters": [],
                       "sort_by": "comprehensive", "count": 2, "generation_count": 2, "page": 1, "page_size": 10, "start_generation": False}
            ranked = post(base + "/api/recommend", payload)
            assert ranked["items"] and ranked["total"] == records
            params = ranked["items"][0]["params"]
            explained = post(base + "/api/price-workbench/predict", {"parameters": params})
            assert math.isfinite(explained["prediction"]["predicted_price_wan"])
            assert explained["explanation"]["model_contributions"] and explained["explanation"]["expert_groups"]
            before = post(base + "/api/evaluate", {"parameters": params})
            key = "rated_thrust_n" if features == 50 else "load_n"
            changed = dict(params); changed[key] = float(changed[key]) + 250
            after = post(base + "/api/evaluate", {"parameters": changed})
            assert abs(before["predicted_price_wan"] - after["predicted_price_wan"]) > 1e-5
            assert math.isfinite(after["capability_score"])
            task = post(base + "/api/generation/request", payload)
            deadline = time.monotonic() + 90
            while task["status"] not in ("completed", "failed") and time.monotonic() < deadline:
                time.sleep(.2)
                task = get(base + "/api/generation-tasks/" + task["task_id"])
            assert task["status"] == "completed", task
            generated = post(base + "/api/recommend", dict(payload, source_mode="generated", generation_batch_id=task["batch_id"]))
            assert generated["total"] > 0
            price_health, effect_health = get(runtime["price_url"] + "/health"), get(runtime["effectiveness_url"] + "/health")
            assert price_health["status"] == "ok" and effect_health["status"] == "ok"
            return {"records": records, "features": features, "status": "PASS", "price_effect_recommendation": True,
                    "chinese_labels": True, "expert_explanation": True, "generation": True, "recalculation": True,
                    "output": str(output.relative_to(package)), "seconds_to_complete": round(time.monotonic() - started, 2),
                    "price_members": json.loads((output / "price" / "training_report.json").read_text(encoding="utf-8"))["member_count"]}
        finally:
            if process.poll() is None:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                try:
                    process.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    process.terminate(); process.wait(timeout=5)
                    raise AssertionError("Launcher did not stop cleanly; inspect owned service processes.")
            if runtime is not None:
                for name in ("price_url", "effectiveness_url", "url"):
                    try:
                        opener.open(runtime[name] + ("health" if name == "url" else "/health"), timeout=2).close()
                    except Exception:
                        continue
                    raise AssertionError("A package service remained running after CTRL_BREAK: " + name)


def replacement_rows():
    labels = ["成品编号", "成品名称", "价格(万元)", "是否支持调速", "载荷(N)", "运行速度(mm/s)", "尺寸(mm)", "工作温度上限(℃)", "材料类型"]
    ids = ["product_id", "product_name", "price_wan", "speed_control", "load_n", "travel_mm_s", "length_mm", "upper_temp_c", "material"]
    rows = []
    for i in range(12):
        load, speed, length, temperature = 2000 + i * 400, 10 + i * 7 % 17, 18 + i * 9 % 12, 70 + i % 4 * 5
        price = round(3 + load * .0012 + speed * .023 + length * .004 + temperature * .003 + (i % 2) * .3 + i * .003, 5)
        rows.append(["NEW%03d" % i, "换表成品%02d" % i, price, "有" if i % 2 else "无", load, speed, length, temperature, "铝" if i % 3 else "钢"])
    return [labels, ids] + rows


def replacement_table():
    rows = replacement_rows()
    rows[1][4] = "ｌｏａｄ＿ｎ\u200b"
    raw = write_workbook_bytes([("隐藏数据", replacement_rows()[:3]),
                                ("专家配置", [["说明", "备注"], ["notes", "not_product_data"]]), ("成品数据", rows)])
    output = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(raw)) as source, zipfile.ZipFile(output, "w") as target:
        for member in source.infolist():
            data = source.read(member.filename)
            if member.filename == "xl/workbook.xml":
                root = ET.fromstring(data)
                namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
                root.find("{%s}sheets/{%s}sheet" % (namespace, namespace)).set("state", "hidden")
                data = ET.tostring(root, encoding="utf-8")
            if member.filename == "xl/worksheets/sheet3.xml":
                data = data.replace(b"</row>", b'<c r="XFD1" s="0"/></row>', 1)
            target.writestr(member, data)
    return output.getvalue()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--zip", required=True)
    parser.add_argument("--extract-root", required=True)
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    archive_path, directory = Path(args.zip).resolve(), Path(args.extract_root).resolve()
    if (ROOT / "deliverables").resolve() not in directory.parents or directory.exists():
        raise ValueError("Choose a new extraction directory under workspace deliverables.")
    directory.mkdir(parents=True)
    with zipfile.ZipFile(str(archive_path)) as archive:
        for member in archive.infolist():
            target = (directory / member.filename).resolve()
            if directory not in target.parents:
                raise ValueError("Unsafe archive path")
        assert archive.testzip() is None
        archive.extractall(str(directory))
    roots = [p for p in directory.iterdir() if p.is_dir()]
    assert len(roots) == 1
    package = roots[0]
    env = dict(os.environ, PYTHONHOME="Z:\\no-host-python", PYTHONPATH="Z:\\no-host-packages", PRODUCT_NO_PAUSE="1")
    env["PATH"] = r"C:\Windows\System32;C:\Windows"
    python = package / "runtime" / "python38" / "python.exe"
    run(["cmd.exe", "/d", "/c", "VERIFY.bat"], package, env)
    runtime = json.loads((package / "logs" / "runtime_verification.json").read_text(encoding="utf-8"))
    assert runtime["status"] == "PASS" and runtime["immutable_files_verified"] > 100
    run(["cmd.exe", "/d", "/c", "TEST_COMPATIBILITY.bat"], package, env)
    compatibility = json.loads((package / "logs" / "portable_compatibility_test_report.json").read_text(encoding="utf-8"))
    assert compatibility["status"] == "PASS"
    micro_tests = None
    if (package / "TEST_MICRO_SAMPLE.bat").is_file():
        run(["cmd.exe", "/d", "/c", "TEST_MICRO_SAMPLE.bat"], package, env)
        micro_tests = json.loads((package / "logs/product_price_micro_test_report.json").read_text(encoding="utf-8"))
        assert micro_tests["status"] == "PASS"
    print("PASS: actual ZIP integrity and embedded runtime; external Python paths blocked", flush=True)
    sample = exercise(package, env, 10, 50, directory / "sample_start.log")
    sample_profile_path = package / sample["output"] / "price/sample_profile.json"
    if micro_tests:
        sample_profile = json.loads(sample_profile_path.read_text(encoding="utf-8"))
        assert sample_profile["active"] and sample_profile["minimum_inner_training_rows"] == 6
    print("PASS: extracted 10 x 50 sample, all three services, generation and shutdown", flush=True)
    old_state = (package / "state" / "latest.json").read_bytes()
    sample_input = (package / "input" / "products.xlsx").read_bytes()
    (package / "input" / "products.xlsx").write_bytes(replacement_table())
    changed = exercise(package, env, 12, 6, directory / "replacement_start.log")
    assert (package / "state" / "latest.json").read_bytes() != old_state
    adaptation = json.loads((package / changed["output"] / "input_adaptation.json").read_text(encoding="utf-8"))
    assert not adaptation["engineering_enabled"] and not adaptation["virtual_data"]
    assert adaptation["input_diagnostics"]["sheet_name"] == "成品数据"
    assert "XFD" in adaptation["input_diagnostics"]["ignored_empty_columns"]
    assert any(cell["normalized"] and cell["parsed"] == "load_n" for cell in adaptation["input_diagnostics"]["header_cells"])
    print("PASS: replace only XLSX with a new 12 x 6 schema, automatic retraining and recommendation", flush=True)
    csv_source = package / "input" / "products.csv"
    text = io.StringIO(); csv.writer(text, delimiter=";").writerows(replacement_rows())
    csv_source.write_bytes(text.getvalue().encode("utf-16"))
    csv_case = exercise(package, env, 12, 6, directory / "csv_start.log", csv_source)
    print("PASS: UTF16 semicolon CSV completes the same three-service pipeline", flush=True)
    valid_state = (package / "state" / "latest.json").read_bytes()
    csv_source.unlink()
    invalid_rows = replacement_rows(); invalid_rows[1][4] = "load-n"
    (package / "input" / "products.xlsx").write_bytes(write_workbook_bytes([("成品数据", invalid_rows)]))
    failed = subprocess.run(["cmd.exe", "/d", "/c", "START.bat", "--no-browser"], cwd=str(package), env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, encoding="utf-8", errors="replace", timeout=30)
    assert failed.returncode != 0 and (package / "state" / "latest.json").read_bytes() == valid_state
    diagnostic = json.loads((package / "logs" / "input_diagnostics.json").read_text(encoding="utf-8"))
    assert diagnostic["status"] == "FAIL" and diagnostic["error_cell"] == "E2"
    (package / "input" / "products.xlsx").write_bytes(sample_input)
    assert (package / json.loads(old_state.decode("utf-8"))["output"]).is_dir()
    report = {"status": "PASS", "archive": str(archive_path), "archive_sha256": sha(archive_path), "bytes": archive_path.stat().st_size,
              "extraction_path": str(package), "python": runtime["python"], "dependency_versions": runtime["dependency_versions"],
              "immutable_files_verified": runtime["immutable_files_verified"], "no_host_python": True, "no_network_install": True,
              "unicode_and_spaces_directory": True, "sample": sample, "replacement_table": changed,
              "compatibility_regressions": compatibility, "micro_sample_regressions": micro_tests, "utf16_csv": csv_case,
              "styled_empty_cells_and_nonfirst_sheet_and_invisible_header": True, "invalid_header_cell_reported": "E2",
              "invalid_input_preserves_previous_build": True, "owned_services_stopped": True, "win7_machine_tested": False}
    Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
