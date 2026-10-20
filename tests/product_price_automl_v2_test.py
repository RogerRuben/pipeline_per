# -*- coding: utf-8 -*-
"""Exercise tiny-wide expert fusion, fold boundaries and actual three services."""
from __future__ import print_function

import copy
import hashlib
import json
import os
import signal
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
from tools.product_pipeline import build, fixture_csv
from tools.product_v2_fixture import tiny_fixture
from tools.product_price_automl import price_dataset
from tools.product_price_automl_v2 import (load_config, estimator, _oof, FitWorker, BudgetExpired, workbook_experts)
from tools.price_expert_fusion_probe import fit_expert_weights
from services.price_service.app import PriceService
from services.price_service.native_bundle import load_bundle, predict, predict_batch


class ProductV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tiny_fixture()
        cls.workbook = ROOT / "tests" / "fixtures" / "product_pipeline_v2" / "virtual_10x50.xlsx"
        cls.data = HistoricalProductOnboarding().analyze("tiny.csv", fixture_csv(cls.fixture), "TINY_V2_TEST", "微样本集成测试", ["-1", "\\", "/"])["data"]
        cls.frame, cls.y, cls.specs, cls.mappings, cls.groups, cls.ids = price_dataset(cls.data, 8)
        cls.holder = tempfile.TemporaryDirectory(prefix="product_v2_test_", dir=str(ROOT / "outputs"))
        cls.directory = Path(cls.holder.name)
        cls.config = copy.deepcopy(cls.fixture["price_config"])
        cls.config["search"].update(trials=13, ensemble_trials=2)
        cls.config["validation"].update(outer_repeats=1, repeats=1)
        cls.config["seed_bagging"]["milestones"] = [2, 4]
        cls.config_path = cls.directory / "config.json"
        cls.config_path.write_text(json.dumps(cls.config), encoding="utf-8")
        cls.main_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in
                           (ROOT / "data" / "protocol_demo.db", ROOT / "services" / "price_service" / "model" / "price_native_bundle.pkl") if p.exists()}
        cls.output = cls.directory / "build"
        cls.manifest = build(input_path=cls.workbook, output_dir=cls.output, product_code="TINY_V2_TEST", product_name="微样本集成测试",
                             price_engine="v2", price_config=cls.config_path, virtual_data=True)
        cls.report = json.loads((cls.output / "price" / "training_report.json").read_text(encoding="utf-8"))
        cls.bundle = load_bundle(cls.output / "price" / "price_native_bundle.pkl")

    @classmethod
    def tearDownClass(cls):
        assert (ROOT / "outputs").resolve() in cls.directory.resolve().parents
        cls.holder.cleanup()

    def test_exact_ten_rows_fifty_attributes_and_workbook_experts(self):
        self.assertEqual(self.frame.shape, (10, 50))
        workbook = HistoricalProductOnboarding().analyze(self.workbook.name, self.workbook.read_bytes(), "TINY_V2_TEST", "微样本集成测试", ["-1", "\\", "/"])["data"]
        self.assertEqual(self.data["agreements"], workbook["agreements"])
        experts = workbook_experts(self.workbook, self.config_path)
        self.assertEqual([x["weight"] for x in experts["ensemble"]["family_priors"]], [.35, .35, .2, .1])
        self.assertEqual(experts["engineering"], self.fixture["price_config"]["engineering"])

    def test_complete_inner_selection_is_audited_without_holdout_leakage(self):
        audit = json.loads((self.output / "price" / "nested_audit.json").read_text(encoding="utf-8"))
        self.assertTrue(audit["completed"] and audit["rerun_full_selection"])
        self.assertTrue(self.report["small_sample_profile"]["active"])
        self.assertEqual(self.report["small_sample_profile"]["minimum_inner_training_rows"], 6)
        self.assertEqual(len(audit["folds"]), 8)
        self.assertTrue(all(fold["metrics"]["r2"] is None for fold in audit["folds"]))
        for component in self.report["small_sample_profile"]["fitted_components"]:
            if "effective_linear_df" in component:
                self.assertLessEqual(component["effective_linear_df"], 3. + 1e-7)
        test_ids, development = set(self.report["holdout_ids"]), set(self.report["development_ids"])
        covered = set()
        for fold in audit["folds"]:
            training, validation = set(fold["training_ids"]), set(fold["validation_ids"])
            self.assertFalse(training & validation or (training | validation) & test_ids)
            self.assertEqual(training | validation, development)
            covered.update(validation)
        self.assertEqual(covered, development)
        self.assertFalse(self.report["evidence_sufficient"])
        self.assertFalse(self.report["business_accuracy_certified"])
        self.assertIsNone(self.report["representative_seed"])

    def test_native_single_batch_and_http_match_with_missing_numeric_and_enum(self):
        service = PriceService(self.output / "price" / "price_native_bundle.pkl")
        batch = predict_batch(self.bundle, [row["params"] for row in self.data["agreements"]])
        self.assertEqual(len(service.schema()["fields"]), 50)
        for index, row in enumerate(self.data["agreements"]):
            direct = sum(member["weight"] * float(self.bundle["models"][member["name"]].predict(self.frame.iloc[[index]])[0])
                         for member in self.bundle["ensemble"]["members"])
            native = predict(self.bundle, row["params"])
            served = service.handle_post("/api/v1/predict", {"parameters": row["params"]})
            self.assertAlmostEqual(native["predicted_price_wan"], direct, places=5)
            self.assertEqual(native["predicted_price_wan"], batch[index]["predicted_price_wan"])
            self.assertEqual(native["predicted_price_wan"], served["prediction"]["predicted_price_wan"])
            self.assertEqual(served["prediction"]["confidence"], "low")
            contributions = served["explanation"]["model_contributions"]
            self.assertAlmostEqual(sum(x["weighted_contribution_wan"] for x in contributions), direct, places=5)
        self.assertEqual(batch[2]["input_status"]["filled_fields"]["comm_latency_ms"]["strategy"], "model_imputer")
        self.assertEqual(batch[5]["input_status"]["filled_fields"]["material_type"]["strategy"], "model_imputer")

    def test_expert_family_share_does_not_grow_when_members_are_duplicated(self):
        y = np.linspace(2, 10, 24)
        first = fit_expert_weights(y, np.column_stack([y * .99, y * 1.01]), expert_groups=[
            {"name": "first", "members": [0], "weight": .7}, {"name": "second", "members": [1], "weight": .3}], max_mape_delta_pp=1)
        repeated = fit_expert_weights(y, np.column_stack([y * .99] * 5 + [y * 1.01]), expert_groups=[
            {"name": "first", "members": list(range(5)), "weight": .7}, {"name": "second", "members": [5], "weight": .3}], max_mape_delta_pp=1)
        self.assertAlmostEqual(first["expert_groups"][0]["learned_weight"], .7, places=5)
        self.assertAlmostEqual(sum(repeated["weights"][:5]), .7, places=5)

    def test_oof_cache_reuses_only_matching_schema_and_protocol(self):
        fixture = self
        class Worker(object):
            calls = 0
            def fit(self, candidate, seed, specs, frame, y, validation):
                self.calls += 1
                self_outer = fixture
                self_outer.assertFalse(set(frame.index) & set(validation.index))
                return np.full(len(validation), np.mean(y)), None
        worker = Worker()
        config = load_config(overrides=self.config)
        candidate = {"model": "engineering_cost"}
        cache = self.directory / "cache"
        first, hit = _oof(self.frame, self.y, self.groups, self.specs, candidate, [101], config, worker, cache, [311])
        self.assertFalse(hit)
        calls = worker.calls
        second, hit = _oof(self.frame, self.y, self.groups, self.specs, candidate, [101], config, worker, cache, [311])
        self.assertTrue(hit); self.assertEqual(worker.calls, calls)
        np.testing.assert_array_equal(first, second)
        amended = copy.deepcopy(self.specs); amended[0]["value_type"] = "number"
        _, hit = _oof(self.frame, self.y, self.groups, amended, candidate, [101], config, worker, cache, [311])
        self.assertFalse(hit); self.assertGreater(worker.calls, calls)

    def test_invalid_settings_and_exhausted_budget_fail_explicitly(self):
        with self.assertRaises(ValueError):
            load_config(overrides={"schema_version": "price-automl-v2-proposal-1.1"})
        with self.assertRaises(ValueError):
            load_config(overrides={"search": {"parameter_ranges": {"ridge": {"unknown": [1, 2]}}}})
        worker = FitWorker(load_config(overrides=self.config), time.monotonic() - 1)
        try:
            with self.assertRaises(BudgetExpired):
                worker.fit({"model": "engineering_cost"}, 101, self.specs, self.frame, self.y, self.frame)
        finally:
            worker.close()

    def test_final_expert_weights_obey_budget_and_seed_policy(self):
        fusion = self.report["fusion"]
        self.assertAlmostEqual(sum(self.report["weights"]), 1, places=7)
        self.assertGreaterEqual(min(self.report["weights"]), -1e-7)
        self.assertLessEqual(fusion["mape_delta_percentage_points"], .1 + 1e-6)
        self.assertLessEqual(fusion["r2_drop"], .005 + 1e-8)
        for row in self.report["seed_report"]:
            self.assertEqual(len(row["seeds"]), len(set(row["seeds"])))
            if row["config"]["model"] not in ("random_forest", "extra_trees", "gbdt"):
                self.assertEqual(len(row["seeds"]), 1)

    def test_fit_timeout_stops_its_worker_without_leaving_queue_threads(self):
        config = load_config(overrides=self.config)
        config["resources"]["fit_timeout_seconds"] = .02
        worker = FitWorker(config, time.monotonic() + 10)
        started = time.monotonic()
        try:
            with self.assertRaises(RuntimeError):
                worker.fit({"model": "engineering_cost"}, 101, self.specs, self.frame, self.y, self.frame)
            self.assertIsNone(worker.process)
            self.assertLess(time.monotonic() - started, 6)
        finally:
            worker.close()

    def test_recommendation_generation_recalculation_and_chinese_schema(self):
        log = self.directory / "server.log"
        env = dict(os.environ, PYTHONIOENCODING="utf-8", IPDEMO_AUTH_ENABLED="0")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get(url):
            with opener.open(url, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        def post(url, payload):
            request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8"))
        with log.open("w", encoding="utf-8") as handle:
            process = subprocess.Popen([sys.executable, str(ROOT / "tools" / "product_pipeline.py"), "serve", "--output", str(self.output),
                                        "--activate-draft", "--no-browser", "--price-port", "0", "--effect-port", "0", "--app-port", "0"],
                                       stdout=handle, stderr=handle, env=env, creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            try:
                deadline = time.monotonic() + 45
                while not (self.output / "running.json").exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        self.fail(log.read_text(encoding="utf-8"))
                    time.sleep(.1)
                base = json.loads((self.output / "running.json").read_text(encoding="utf-8"))["url"].rstrip("/")
                bootstrap = get(base + "/api/bootstrap")
                self.assertEqual(bootstrap["counts"]["historical"], 10)
                self.assertEqual(len(bootstrap["parameters"]), 50)
                self.assertTrue(bootstrap["integration"]["product_ready"])
                schema = get(base + "/api/price-workbench/schema")
                self.assertTrue(all(any("\u3400" <= char <= "\u9fff" for char in field["field_label"]) for field in schema["fields"]))
                payload = {"session_id": "v2-test", "source_mode": "historical", "selected_tags": [], "indicator_filters": [],
                           "sort_by": "comprehensive", "count": 2, "generation_count": 2, "page": 1, "page_size": 10, "start_generation": False}
                ranked = post(base + "/api/recommend", payload)
                self.assertTrue(ranked["items"])
                params = ranked["items"][0]["params"]
                before = post(base + "/api/evaluate", {"parameters": params})
                after = post(base + "/api/evaluate", {"parameters": dict(params, rated_thrust_n=params["rated_thrust_n"] + 500)})
                self.assertNotAlmostEqual(before["predicted_price_wan"], after["predicted_price_wan"], places=4)
                self.assertEqual(len(after["coupling_assessments"]), 3)
                explained = post(base + "/api/price-workbench/predict", {"parameters": params})
                self.assertTrue(explained["explanation"]["expert_groups"])
                task = post(base + "/api/generation/request", payload)
                deadline = time.monotonic() + 45
                while task["status"] not in ("completed", "failed") and time.monotonic() < deadline:
                    time.sleep(.1); task = get(base + "/api/generation-tasks/" + task["task_id"])
                self.assertEqual(task["status"], "completed", str(task))
                generated = post(base + "/api/recommend", dict(payload, source_mode="generated", generation_batch_id=task["batch_id"]))
                self.assertGreater(generated["total"], 0)
            finally:
                if process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.terminate(); process.wait(timeout=5)
        for path, digest in self.main_hashes.items():
            self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ProductV2Tests))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "product_price_automl_v2_test_report.json").write_text(json.dumps({"status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
