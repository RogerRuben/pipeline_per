# -*- coding: utf-8 -*-
"""Validate actual TabPFN artifacts and a Python 3.8 HTTP acceptance client."""
from __future__ import print_function

import argparse
import copy
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from app.historical_onboarding import HistoricalProductOnboarding
from services.price_service.native_bundle import load_bundle, predict, predict_batch
from services.tabpfn_price_service.app import TabPFNPriceService
from tools.product_price_tabpfn import load_config
from tools.product_price_automl import price_dataset

OUTPUT = ROOT / "outputs" / "product_tabpfn_10x50_final"
WIN7_PYTHON = ROOT / "runtime" / "tabpfn38" / "python.exe"


def port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=120) as response:
        return json.loads(response.read().decode("utf-8"))


def post(url, payload):
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=180) as response:
        return json.loads(response.read().decode("utf-8"))


def stop(process):
    if process.poll() is None:
        process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.terminate(); process.wait(timeout=5)


class TabPFNTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (ROOT / "outputs").mkdir(exist_ok=True)
        cls.output = OUTPUT
        cls.report = json.loads((OUTPUT / "price" / "training_report.json").read_text(encoding="utf-8"))
        cls.manifest = json.loads((OUTPUT / "prototype_manifest.json").read_text(encoding="utf-8"))
        path = ROOT / "tests" / "fixtures" / "product_pipeline_v2" / "virtual_10x50.xlsx"
        cls.data = HistoricalProductOnboarding().analyze(path.name, path.read_bytes(), cls.manifest["product_code"], cls.manifest["product_name"], ["-1", "\\", "/"])["data"]
        cls.frame, cls.y, cls.specs, _, _, cls.ids = price_dataset(cls.data, 8)
        cls.bundle = load_bundle(OUTPUT / "price" / "price_native_bundle.pkl")
        cls.service = TabPFNPriceService(OUTPUT / "price" / "price_native_bundle.pkl")

    def test_input_and_chinese_schema(self):
        self.assertEqual(self.frame.shape, (10, 50))
        fields = self.service.schema()["fields"]
        self.assertEqual(len(fields), 50)
        self.assertEqual(len({field["field_name"] for field in fields}), 50)
        self.assertTrue(all(any("\u3400" <= c <= "\u9fff" for c in field["field_label"]) for field in fields))

    def test_outer_audit_and_holdout_boundaries(self):
        audit = json.loads((OUTPUT / "price" / "nested_audit.json").read_text(encoding="utf-8"))
        self.assertTrue(audit["completed"] and audit["rerun_full_selection"])
        development, holdout = set(self.report["development_ids"]), set(self.report["holdout_ids"])
        for fold in audit["folds"]:
            training, validation = set(fold["training_ids"]), set(fold["validation_ids"])
            self.assertFalse(training & validation or (training | validation) & holdout)
            self.assertEqual(training | validation, development)
        self.assertFalse(self.report["business_accuracy_certified"])
        self.assertFalse(self.report["evidence_sufficient"])
        if self.report.get("validation_protocol") == "group_loo":
            self.assertEqual(self.report["minimum_inner_training_groups"], 8)
            self.assertEqual(len(audit["folds"]), 10)
            self.assertEqual(len(development), 10)
            self.assertFalse(holdout)
            self.assertIsNone(self.report["holdout_metrics"]["r2"])
            self.assertTrue(all(fold["metrics"]["r2"] is None for fold in audit["folds"]))

    def test_real_tabpfn_benchmark_is_distinct_from_fusion(self):
        self.assertEqual(self.report["runtime"]["tabpfn"], "2.0.9")
        self.assertFalse(self.report["failed_candidates"])
        comparison = self.report["tabpfn_only"]
        r2 = comparison["nested_cv_metrics"]["mean_r2"] if self.report.get("validation_protocol") == "group_loo" else comparison["holdout_metrics"]["r2"]
        self.assertTrue(np.isfinite(r2))
        standalone = load_bundle(OUTPUT / "price" / "tabpfn_only_bundle.pkl")
        self.assertTrue(all(item["family"] == "tabpfn" for item in standalone["automl_explanation"]["member_ledger"]))
        self.assertTrue(np.isfinite(predict(standalone, self.data["agreements"][0]["params"])["predicted_price_wan"]))

    def test_seed_averaging_and_expert_error_budget(self):
        fusion = self.report["fusion"]
        self.assertAlmostEqual(sum(self.report["weights"]), 1, places=7)
        self.assertLessEqual(fusion["mape_delta_percentage_points"], .1 + 1e-6)
        self.assertLessEqual(fusion["r2_drop"], .005 + 1e-6)
        for row in self.report["seed_report"]:
            self.assertEqual(len(row["seeds"]), len(set(row["seeds"])))
            if row["config"]["family"] == "tabpfn" and row["seed_stop_reason"] != "screening_only":
                self.assertGreaterEqual(len(row["seeds"]), 2)
        self.assertTrue(all(item["weight"] > 1e-9 for item in self.report["member_ledger"]))

    def test_native_single_batch_service_and_missing_values_agree(self):
        params = [row["params"] for row in self.data["agreements"]]
        batch = predict_batch(self.bundle, params)
        for index in (0, 2, 5):
            native = predict(self.bundle, params[index])
            served = self.service.handle_post("/api/v1/predict", {"parameters": params[index]})
            self.assertAlmostEqual(native["predicted_price_wan"], batch[index]["predicted_price_wan"], places=4)
            self.assertEqual(native["predicted_price_wan"], served["prediction"]["predicted_price_wan"])
            contributions = served["explanation"]["model_contributions"]
            self.assertAlmostEqual(sum(item["weighted_contribution_wan"] for item in contributions), native["predicted_price_wan"], places=4)
            self.assertEqual(served["prediction"]["confidence"], "low")
        self.assertEqual(batch[2]["input_status"]["filled_fields"]["comm_latency_ms"]["strategy"], "model_imputer")
        self.assertEqual(batch[5]["input_status"]["filled_fields"]["material_type"]["strategy"], "model_imputer")

    def test_unknown_config_and_nonfinite_input_are_rejected(self):
        with self.assertRaises(ValueError):
            load_config(overrides={"unsupported_option": True})
        with self.assertRaises(ValueError):
            load_config(overrides={"tabpfn": {"variants": [{"target_transform": "identity", "output_type": "mode"}]}})
        params = dict(self.data["agreements"][0]["params"], rated_thrust_n=float("inf"))
        with self.assertRaises(ValueError):
            predict(self.bundle, params)

    def test_real_http_and_python38_recommendation_client(self):
        self.assertTrue(WIN7_PYTHON.is_file(), "Install the offline Win7 client runtime first")
        with tempfile.TemporaryDirectory(prefix="tabpfn_client_test_", dir=str(ROOT / "outputs")) as temporary:
            directory = Path(temporary)
            copied = directory / "prototype"
            shutil.copytree(OUTPUT, copied, ignore=shutil.ignore_patterns("oof_cache", "*.npz", "price", "ui_verification", "running.json", "client_running.json"))
            price_port = port()
            env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", HF_HUB_OFFLINE="1", HF_HUB_DISABLE_TELEMETRY="1")
            with (directory / "price.log").open("w", encoding="utf-8") as price_log, (directory / "client.log").open("w", encoding="utf-8") as client_log:
                price = subprocess.Popen([sys.executable, "-m", "services.tabpfn_price_service.app", "--model", str(OUTPUT / "price" / "price_native_bundle.pkl"), "--port", str(price_port)],
                    cwd=str(ROOT), env=env, stdout=price_log, stderr=price_log, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                client = None
                try:
                    price_url = "http://127.0.0.1:%d" % price_port
                    deadline = time.monotonic() + 60
                    while True:
                        self.assertIsNone(price.poll(), (directory / "price.log").read_text(encoding="utf-8"))
                        try:
                            if get(price_url + "/health")["status"] == "ok":
                                break
                        except OSError:
                            if time.monotonic() > deadline:
                                self.fail("Price service readiness timeout")
                            time.sleep(.2)
                    response = post(price_url + "/api/v1/predict/batch", {"items": [{"candidate_id": "sample", "parameters": self.data["agreements"][0]["params"]}]})
                    self.assertEqual(response["count"], 1)
                    client = subprocess.Popen([str(WIN7_PYTHON), str(ROOT / "tools" / "run_tabpfn_win7_client.py"), "--output", str(copied), "--price-url", price_url,
                        "--effect-port", "0", "--app-port", "0", "--no-browser"], cwd=str(ROOT), env=env, stdout=client_log, stderr=client_log,
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
                    deadline = time.monotonic() + 45
                    while not (copied / "client_running.json").exists():
                        self.assertIsNone(client.poll(), (directory / "client.log").read_text(encoding="utf-8"))
                        if time.monotonic() > deadline:
                            self.fail("Client readiness timeout")
                        time.sleep(.2)
                    base = json.loads((copied / "client_running.json").read_text(encoding="utf-8"))["url"].rstrip("/")
                    bootstrap = get(base + "/api/bootstrap")
                    self.assertEqual(bootstrap["counts"]["historical"], 10)
                    self.assertEqual(len(bootstrap["parameters"]), 50)
                    self.assertTrue(bootstrap["integration"]["product_ready"])
                    payload = {"session_id": "tabpfn-client", "source_mode": "historical", "selected_tags": [], "indicator_filters": [],
                               "sort_by": "comprehensive", "generation_count": 1, "count": 2, "page": 1, "page_size": 10, "start_generation": False}
                    ranked = post(base + "/api/recommend", payload)
                    self.assertTrue(ranked["items"])
                    params = ranked["items"][0]["params"]
                    before = post(base + "/api/evaluate", {"parameters": params})
                    after = post(base + "/api/evaluate", {"parameters": dict(params, rated_thrust_n=params["rated_thrust_n"] + 500)})
                    self.assertNotAlmostEqual(before["predicted_price_wan"], after["predicted_price_wan"], places=4)
                    self.assertEqual(len(after["coupling_assessments"]), 3)
                    explained = post(base + "/api/price-workbench/predict", {"parameters": params})
                    deployed_tabpfn = any(item["family"] == "tabpfn" for item in self.report["member_ledger"])
                    self.assertEqual(any(item["family"] == "tabpfn" for item in explained["explanation"]["model_contributions"]), deployed_tabpfn)
                    if self.report.get("price_attribute_scores"):
                        ratings = explained["explanation"]["price_attribute_scores"]
                        self.assertEqual(len(ratings), 50)
                        self.assertTrue(all(1 <= item["score"] <= 5 for item in ratings))
                    task = post(base + "/api/generation/request", payload)
                    deadline = time.monotonic() + 120
                    while task["status"] not in ("completed", "failed") and time.monotonic() < deadline:
                        time.sleep(1)
                        task = get(base + "/api/generation-tasks/" + task["task_id"])
                    self.assertEqual(task["status"], "completed", str(task))
                    generated = post(base + "/api/recommend", dict(payload, source_mode="generated", generation_batch_id=task["batch_id"]))
                    self.assertGreater(generated["total"], 0)
                finally:
                    if client:
                        stop(client)
                    stop(price)
            self.assertIn((ROOT / "outputs").resolve(), directory.resolve().parents)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--win7-python", type=Path, default=WIN7_PYTHON)
    args = parser.parse_args()
    OUTPUT, WIN7_PYTHON = args.output.resolve(), args.win7_python.resolve()
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(TabPFNTests))
    report = {"status": "PASS" if result.wasSuccessful() else "FAIL", "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
              "win7_python_protocol_verified": result.wasSuccessful(), "win7_os_verified": False}
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "product_price_tabpfn_test_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
