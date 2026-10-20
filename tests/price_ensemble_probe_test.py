# -*- coding: utf-8 -*-
"""Verify constrained fusion and mixed native pipelines on the Win7 package stack."""
from __future__ import print_function

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np

from app.historical_onboarding import HistoricalProductOnboarding
from services.price_service.app import PriceService
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import load_bundle, predict
from tools.price_ensemble_probe import estimator, loss, paired_group_bootstrap, weights_for
from tools.product_pipeline import fixture_csv, virtual_fixture
from tools.product_price_automl import native_field_metadata, price_dataset


class EnsembleProbeTests(unittest.TestCase):
    def test_complementary_members_recover_price_with_feasible_weights(self):
        y = np.asarray([2.0, 3.0, 5.0, 9.0])
        deviation = np.asarray([.4, .3, .5, .8])
        bank = np.column_stack([y + deviation, y - deviation, y + 1.0])
        best_single = min(loss(y, bank[:, index]) for index in range(bank.shape[1]))
        for method in ("greedy_selection", "regularized_weights"):
            weights = weights_for(method, y, bank)
            self.assertGreaterEqual(np.min(weights), 0)
            self.assertAlmostEqual(np.sum(weights), 1)
            self.assertLess(loss(y, bank.dot(weights)), best_single / 10)
            self.assertGreater(weights[0], 0)
            self.assertGreater(weights[1], 0)

    def test_paired_bootstrap_reports_conditional_uncertainty(self):
        y = np.asarray([2.0, 2.0, 4.0, 6.0, 8.0])
        groups = np.asarray([0, 0, 1, 2, 3])
        identical = paired_group_bootstrap(y, y, y, groups, draws=100)
        self.assertTrue(identical["contains_zero"])
        self.assertEqual(identical["ci95"], [0.0, 0.0])
        better = paired_group_bootstrap(y, y, y + 1, groups, draws=100)
        self.assertLess(better["ci95"][1], 0)
        self.assertEqual(better["independent_groups"], 4)

    def test_mixed_member_transforms_missing_categories_and_service_equivalence(self):
        fixture = virtual_fixture()
        category_column = fixture["field_ids"].index("power_type")
        fixture["rows"][0][category_column] = ""
        data = HistoricalProductOnboarding().analyze("mixed.csv", fixture_csv(fixture), "ENSEMBLE_TEST", "混合模型验证",
                                                     ["-1", "\\", "/"])["data"]
        frame, y, specs, mappings, groups, ids = price_dataset(data)
        metadata = native_field_metadata(frame, specs, mappings)
        self.assertIn(metadata["power_type"]["default_value"], ["24V", "48V"])
        self.assertFalse(metadata["power_type"]["required"])
        names, weights = ["lasso", "gbdt_log", "ridge_interactions"], [.2, .3, .5]
        fitted = dict((name, estimator(name, 101, specs).fit(frame, y)) for name in names)
        installed = ROOT / "services" / "price_service" / "model" / "price_native_bundle.pkl"
        before = hashlib.sha256(installed.read_bytes()).hexdigest() if installed.exists() else None
        (ROOT / "outputs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="ensemble_contract_test_", dir=str(ROOT / "outputs")) as directory:
            self.assertIn((ROOT / "outputs").resolve(), Path(directory).resolve().parents)
            path = Path(directory) / "price_native_bundle.pkl"
            export_from_notebook(dict(fitted, X_train=frame), output=path, product_code="ENSEMBLE_TEST", product_name="混合模型验证",
                                 model_version="mixed-transform-test", target_divisor_to_wan=1, field_metadata=metadata,
                                 model_variables=dict((name, name) for name in names), ensemble_model_names=names,
                                 ensemble_weights=weights, strict=True, model_source="namespace", model_output_transform="direct")
            bundle = load_bundle(path)
            service = PriceService(path)
            self.assertEqual(service.schema()["model_count"], 3)
            for index, record in enumerate(data["agreements"][:8]):
                direct = sum(weight * float(fitted[name].predict(frame.iloc[[index]])[0]) for name, weight in zip(names, weights))
                native = predict(bundle, record["params"])
                served = service.handle_post("/api/v1/predict", {"product_code": "ENSEMBLE_TEST", "parameters": record["params"]})
                self.assertAlmostEqual(native["predicted_price_wan"], direct, places=5)
                self.assertAlmostEqual(served["prediction"]["predicted_price_wan"], direct, places=5)
                if index == 0:
                    self.assertIn(native["input_status"]["filled_fields"]["power_type"]["value"], ["24V", "48V"])
        if before is not None:
            self.assertEqual(before, hashlib.sha256(installed.read_bytes()).hexdigest())


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(EnsembleProbeTests))
    report = {"status": "PASS" if result.wasSuccessful() else "FAIL", "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "price_ensemble_probe_test_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
