# -*- coding: utf-8 -*-
"""Contract, validation isolation and real three-service prototype regression."""
from __future__ import print_function

import copy
import hashlib
import json
import os
import re
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
import pandas as pd
from app.historical_onboarding import HistoricalProductOnboarding
from app.product_table import product_table_rows
from app.product_releases import ProductReleaseService
from app.model_service_client import DegradedServiceRuntime
from app.store import Store
from tools.product_pipeline import build, fixture_csv, virtual_fixture
from tools.product_price_automl import grouped_folds, make_pipeline, price_dataset
from tools.product_effectiveness_bootstrap import build_effectiveness, load_config
from services.price_service.native_bundle import load_bundle, predict


class ProductPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = virtual_fixture()
        cls.raw = fixture_csv(cls.fixture)
        cls.report = HistoricalProductOnboarding().analyze("demo.csv", cls.raw, "PIPELINE_TEST", "虚拟流水线测试", ["-1", "\\", "/"])
        (ROOT / "outputs").mkdir(exist_ok=True)
        cls.holder = tempfile.TemporaryDirectory(prefix="product_pipeline_test_", dir=str(ROOT / "outputs"))
        cls.output = Path(cls.holder.name) / "build"
        cls.main_hashes = dict((str(p), hashlib.sha256(p.read_bytes()).hexdigest()) for p in
                              [ROOT / "data" / "protocol_demo.db", ROOT / "services" / "price_service" / "model" / "price_native_bundle.pkl",
                               ROOT / "services" / "effectiveness_service" / "model" / "current" / "effectiveness_runtime_manifest.json"] if p.exists())

    @classmethod
    def tearDownClass(cls):
        assert (ROOT / "outputs").resolve() in Path(cls.holder.name).resolve().parents
        cls.holder.cleanup()

    def test_dual_header_preserves_ids_and_labels(self):
        report = self.report
        self.assertEqual(report["row_count"], 164)
        self.assertEqual(report["attribute_count"], 12)
        self.assertEqual(report["data"]["parameters"][2]["parameter_id"], "rated_thrust_n")
        self.assertEqual(report["data"]["parameters"][2]["label"], "额定推力")
        self.assertNotIn("response_time_ms", report["data"]["agreements"][0]["params"])
        self.assertEqual(report["data"]["agreements"][0]["params"]["emergency_unlock"], 0)
        self.assertEqual(report["data"]["agreements"][0]["params"]["power_type"], "48V")
        amended = copy.deepcopy(self.fixture)
        amended["field_ids"][5] = "Rated_Thrust_N"
        amended_report = HistoricalProductOnboarding().analyze("case.csv", fixture_csv(amended), "TEST", "测试")
        self.assertIn("Rated_Thrust_N", amended_report["data"]["agreements"][0]["params"])

    def test_bad_ids_are_rejected_without_renaming(self):
        for code in ("rated-thrust", "额定推力", "1thrust", "emergency_unlock", "EMERGENCY_UNLOCK", ""):
            amended = copy.deepcopy(self.fixture)
            amended["field_ids"][5] = code
            with self.assertRaises(ValueError):
                product_table_rows("bad.csv", fixture_csv(amended), require_dual_header=True)

    def test_legacy_ascii_text_records_are_not_headers(self):
        raw = "成品编号,成品名称,材料,工艺\nS001,Basic,Steel,Milled\nS002,Light,Aluminium,Cast\n".encode("utf-8")
        table = product_table_rows("legacy.csv", raw)
        self.assertEqual(table["header_rows"], 1)
        self.assertEqual(len(table["rows"]), 2)
        self.assertIsNone(table["field_ids"])

    def test_xlsx_and_csv_use_identical_contract(self):
        workbook = ROOT / "tests" / "fixtures" / "product_pipeline" / "virtual_products_dual_header.xlsx"
        self.assertTrue(workbook.exists(), "双行表头XLSX示例必须随开发结果生成")
        report = HistoricalProductOnboarding().analyze(workbook.name, workbook.read_bytes(), "PIPELINE_TEST", "虚拟流水线测试", ["-1", "\\", "/"])
        self.assertEqual(report["data"]["agreements"], self.report["data"]["agreements"])
        config = load_config(workbook)
        self.assertEqual(len(config["couplings"]), 3)
        self.assertEqual(config["attributes"]["rated_thrust_n"]["weight"], 2)

    def test_duplicate_inputs_stay_in_same_validation_fold(self):
        frame, y, specs, mappings, groups, ids = price_dataset(self.report["data"])
        self.assertEqual(groups[0], groups[-4])
        for train, validation in grouped_folds(groups, 3, 42):
            self.assertFalse(set(groups[train]) & set(groups[validation]))
        amended = copy.deepcopy(self.report["data"])
        amended["agreements"][0]["historical_price_wan"] = 0
        with self.assertRaises(ValueError):
            price_dataset(amended)

    def test_preprocessing_uses_only_fit_rows(self):
        specs = [{"value_type": "number"}, {"value_type": "enum"}, {"value_type": "boolean"}]
        frame = pd.DataFrame([[1, 0, 0], [3, 0, 1], [np.nan, 1, 0], [5, np.nan, np.nan]])
        model = make_pipeline({"model": "ridge", "alpha": 1}, 42, specs).fit(frame, np.asarray([2, 4, 3, 6]))
        preparation = model.named_steps["prepare"]
        self.assertEqual(preparation.named_transformers_["numeric"].named_steps["imputer"].statistics_[0], 3)
        self.assertEqual(preparation.named_transformers_["boolean"].named_steps["imputer"].statistics_[0], 0)
        fitted_categories = preparation.named_transformers_["categorical"].named_steps["encode"].categories_[0].tolist()
        self.assertEqual(fitted_categories, [0, 1])
        self.assertTrue(np.isfinite(model.predict(pd.DataFrame([[1000000, 99, np.nan]]))[0]))
        self.assertEqual(preparation.named_transformers_["numeric"].named_steps["imputer"].statistics_[0], 3)

    def test_effectiveness_coupling_changes_when_relationship_breaks(self):
        data = copy.deepcopy(self.report["data"])
        bundle, report = build_effectiveness(data, Path(self.holder.name) / "effect", self.fixture["effectiveness_config"])
        from app.model_runtime import EffectivenessBundle
        runtime = EffectivenessBundle(bundle)
        base = dict(data["agreements"][1]["params"])
        broken = dict(base, peak_thrust_n=4500)
        self.assertLess(runtime.evaluate(broken)["feasibility_probability"], runtime.evaluate(base)["feasibility_probability"])
        self.assertEqual(len(report["coupling_models"]), 3)
        self.assertFalse(report["expert_trained"])
        self.assertFalse(report["feasibility_calibrated"])
        from services.effectiveness_service.app import SnapshotBackend
        from app.model_service_client import _RemoteBundleView, _remote_field
        schema = SnapshotBackend(Path(self.holder.name) / "effect" / "effectiveness_bundle.json").schema()
        remote = _RemoteBundleView(features=[_remote_field(field) for field in schema["fields"]],
                                   couplings=schema["coupling_models"])
        self.assertEqual(len(remote.couplings), 3, "推荐生成器必须能获得效能服务的耦合范围")
        for model in remote.couplings:
            local_band, remote_band = runtime.coupling_band(model, base), remote.coupling_band(model, base)
            for name in ("predicted", "lower", "upper"):
                self.assertAlmostEqual(local_band[name], remote_band[name])
        with self.assertRaises(ValueError):
            build_effectiveness(data, Path(self.holder.name) / "bad_effect", {"couplings": [{"source": "unknown", "target": "mass_kg"}]})

    def test_three_service_pipeline_and_explicit_recalculation(self):
        manifest = build(output_dir=self.output, demo=True, product_code="PIPELINE_TEST", product_name="虚拟流水线测试",
                         models=["ridge", "extra_trees", "gbdt"], trials=6, timeout_seconds=60, target_mape=0, target_r2=1)
        self.assertFalse(manifest["targets_met"], "不能把未达到的指标目标标为通过")
        report = json.loads((self.output / "price" / "training_report.json").read_text(encoding="utf-8"))
        self.assertTrue(report["holdout_evaluated_before_final_refit"])
        params_by_id = dict((r["agreement_id"], r["params"]) for r in self.report["data"]["agreements"])
        dev_signatures = {json.dumps(params_by_id[x], sort_keys=True) for x in report["development_ids"]}
        test_signatures = {json.dumps(params_by_id[x], sort_keys=True) for x in report["holdout_ids"]}
        self.assertFalse(dev_signatures & test_signatures)
        model_path = self.output / manifest["price_model"]
        native = load_bundle(model_path)
        native_manifest = json.loads(Path(str(model_path) + ".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(native_manifest["bundle_sha256"], hashlib.sha256(model_path.read_bytes()).hexdigest())
        workspace = self.output / "recommendation"
        runtime = DegradedServiceRuntime(None, "PIPELINE_TEST", "虚拟流水线测试")
        store = Store(workspace / "data" / "protocol_demo.db", workspace / "data" / "unused.csv", runtime)
        self.assertTrue(store.is_empty())
        self.assertEqual(store.get_product_release(manifest["release_id"])["status"], "validated")
        exported = json.loads((self.output / "business_release.iprelease.json").read_text(encoding="utf-8"))
        exported["data"]["products"][0]["product_name"] = "被改动"
        with self.assertRaises(ValueError):
            ProductReleaseService(store, runtime).import_package(json.dumps(exported, ensure_ascii=False).encode("utf-8"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def get_json(url):
            with opener.open(url, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        def post_json(url, payload):
            request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            with opener.open(request, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        log_path = Path(self.holder.name) / "launcher.log"
        environment = dict(os.environ, PYTHONIOENCODING="utf-8", IPDEMO_AUTH_ENABLED="0")
        with log_path.open("w", encoding="utf-8") as handle:
            process = subprocess.Popen([sys.executable, str(ROOT / "tools" / "product_pipeline.py"), "serve", "--output", str(self.output),
                                        "--activate-draft", "--no-browser", "--price-port", "0", "--effect-port", "0", "--app-port", "0"],
                                       stdout=handle, stderr=handle, env=environment,
                                       creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
            running_path = self.output / "running.json"
            try:
                deadline = time.monotonic() + 45
                while not running_path.exists():
                    if process.poll() is not None:
                        self.fail("三服务启动失败：%s" % log_path.read_text(encoding="utf-8"))
                    if time.monotonic() > deadline:
                        self.fail("三服务启动超时")
                    time.sleep(.1)
                running = json.loads(running_path.read_text(encoding="utf-8"))
                url = running["url"].rstrip("/")
                health = get_json(url + "/api/health")
                self.assertEqual(health["manifest"]["effectiveness"]["coupling_model_count"], 3)
                bootstrap = get_json(url + "/api/bootstrap")
                self.assertTrue(bootstrap["integration"]["product_ready"])
                self.assertEqual(bootstrap["counts"]["historical"], 164)
                self.assertTrue(all(re.search(r"[\u3400-\u9fff]", p["label"]) for p in bootstrap["parameters"]))
                base_request = {"session_id": "prototype-test", "source_mode": "historical", "selected_tags": ["BOOT_TAG_emergency_unlock"],
                                "indicator_filters": [], "indicator_filter_mode": "all", "sort_by": "comprehensive", "count": 2,
                                "generation_count": 2, "page": 1, "page_size": 10, "start_generation": False}
                ranked = post_json(url + "/api/recommend", base_request)
                self.assertTrue(ranked["items"])
                self.assertEqual(ranked["recommendation_mode"], "model_evaluated")
                first = ranked["items"][0]
                self.assertEqual(first["params"]["emergency_unlock"], 1)
                base = post_json(url + "/api/evaluate", {"parameters": first["params"]})
                changed_params = dict(first["params"], rated_thrust_n=first["params"]["rated_thrust_n"] + 500)
                changed = post_json(url + "/api/evaluate", {"parameters": changed_params})
                self.assertGreater(abs(changed["predicted_price_wan"] - base["predicted_price_wan"]), 1e-5)
                self.assertEqual(len(changed["coupling_assessments"]), 3)
                task = post_json(url + "/api/generation/request", base_request)
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:
                    task = get_json(url + "/api/generation-tasks/" + task["task_id"])
                    if task["status"] in ("completed", "failed"):
                        break
                    time.sleep(.15)
                self.assertEqual(task["status"], "completed", str(task))
                self.assertTrue(task.get("batch_id"))
                generated = post_json(url + "/api/recommend", dict(base_request, source_mode="generated", generation_batch_id=task["batch_id"]))
                self.assertGreater(generated["total"], 0)
                self.assertTrue(all(item["is_generated"] for item in generated["items"]))
                # Direct native inference and HTTP use the same preprocessing and model.
                direct = predict(native, changed_params)
                self.assertAlmostEqual(direct["predicted_price_wan"], changed["predicted_price_wan"], places=5)
            finally:
                if process.poll() is None:
                    if os.name == "nt":
                        import signal
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        import signal
                        process.send_signal(signal.SIGINT)
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.terminate()
                        process.wait(timeout=5)
        for filename, digest in self.main_hashes.items():
            self.assertEqual(hashlib.sha256(Path(filename).read_bytes()).hexdigest(), digest)


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ProductPipelineTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "product_pipeline_test_report.json").write_text(json.dumps({"status": "PASS" if result.wasSuccessful() else "FAIL",
         "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
