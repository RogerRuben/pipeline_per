# -*- coding: utf-8 -*-
"""Check price-attribute score semantics and feature ownership across runtimes."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from app.xlsx_utils import write_workbook_bytes
from services.tabpfn_price_service.estimators import TabPFNContextRegressor, AttributePriorRidge
from tools.product_price_tabpfn import load_config, candidates, factory, _oof
from tools.product_r4_input import ratings_from_workbook
from tools.product_effectiveness_bootstrap import load_config as effect_config


class AttributeScoreTests(unittest.TestCase):
    def setUp(self):
        (ROOT / "outputs").mkdir(exist_ok=True)
        self.specs = [{"parameter_id": "x%d" % i, "label": "价格属性%d" % i, "value_type": "enum" if i == 2 else "number"} for i in range(4)]
        self.config = load_config(overrides={"expert": {"scores": {"x0": 5, "x1": 1, "x2": 4}, "strengths": [0, 1]}})

    def test_attribute_score_range_and_neutral_missing_score(self):
        self.assertEqual(self.config["ensemble"]["family_priors"], [])
        self.assertEqual(self.config["ensemble"]["mode"], "data_only")
        for value in (0, 6, .5, True, float("nan")):
            with self.assertRaises(ValueError):
                load_config(overrides={"expert": {"scores": {"x0": value}}})
        self.assertEqual(self.config["expert"]["scores"].get("x3", 3), 3)

    def test_workbook_scores_keep_records_without_changing_effect_preferences(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT / "outputs")) as temporary:
            root = Path(temporary)
            self.assertIn((ROOT / "outputs").resolve(), root.resolve().parents)
            p = root / "scores.xlsx"
            p.write_bytes(write_workbook_bytes([("价格属性评分", [["英文代号", "中文名称", "价格重要性评分", "专家编号"],
                ["x0", "价格属性0", 5, "甲"], ["x0", "价格属性0", 3, "乙"]])]))
            scores, records, _, _ = ratings_from_workbook(p, {"x0"})
            self.assertEqual(scores, {"x0": 4})
            self.assertEqual([row["score"] for row in records], [5, 3])
            self.assertEqual(effect_config(p, allow_legacy=False)["attributes"], {})

    def test_tabpfn_keeps_all_features_and_adds_expert_price_subset(self):
        pool = candidates(self.config, self.specs)
        tab = [row for row in pool if row["family"] == "tabpfn"]
        self.assertTrue(any("feature_subset" not in row for row in tab))
        self.assertTrue(any(row.get("feature_subset") == ["x0", "x2"] for row in tab))
        self.assertTrue(any(row.get("expert_strength") == 0 for row in pool if row["model"] == "attribute_ridge"))

    def test_scaling_is_applied_after_fold_preprocessing_and_changes_regularized_fit(self):
        rng = np.random.RandomState(23)
        X = rng.normal(size=(12, 4)); X[:, 2] = np.arange(12) % 2
        y = 12 + 1.8 * X[:, 0] + .2 * X[:, 1]
        candidate = {"model": "attribute_ridge", "family": "linear", "alpha": 2., "expert_strength": 1, "view": "all"}
        guided = factory(candidate, 101, self.specs, self.config).fit(X[:10], y[:10])
        unweighted = factory(dict(candidate, expert_strength=0), 101, self.specs, self.config).fit(X[:10], y[:10])
        self.assertIsInstance(guided, AttributePriorRidge)
        self.assertGreater(guided.preparation_.multipliers[0], guided.preparation_.multipliers[1])
        self.assertGreater(np.max(np.abs(guided.predict(X) - unweighted.predict(X))), 1e-5)
        before = copy.deepcopy(guided.preparation_.defaults)
        guided.predict(X[10:] * 100)
        self.assertEqual(guided.preparation_.defaults, before)
        self.assertEqual(len(guided.explanation(X)["members"]), 1)

    def test_tabpfn_subset_remaps_categories_and_survives_old_context(self):
        X = np.arange(16, dtype=float).reshape(4, 4)
        recorded = {}
        class FakeRegressor:
            def __init__(self, **kwargs): recorded.update(kwargs)
            def fit(self, values, targets): recorded["X"] = values.copy(); return self
            def predict(self, values, output_type="mean"):
                recorded["predicted_X"] = values.copy(); return np.full(len(values), 10.)
        model = TabPFNContextRegressor(feature_indices=[0, 2], categorical_indices=[2]).fit(X, [10, 11, 12, 13])
        with patch("tabpfn.TabPFNRegressor", FakeRegressor):
            model.predict(X)
        self.assertEqual(recorded["categorical_features_indices"], [1])
        np.testing.assert_array_equal(recorded["X"], X[:, [0, 2]])
        self.assertIsNone(model.__getstate__()["_model"])
        legacy = TabPFNContextRegressor().fit(X, [10, 11, 12, 13])
        del legacy.feature_indices
        with patch("tabpfn.TabPFNRegressor", FakeRegressor): legacy.predict(X)
        self.assertEqual(recorded["X"].shape[1], 4)

    def test_micro_validation_keeps_nine_training_groups_and_duplicate_inputs_together(self):
        import pandas as pd
        cfg = load_config(overrides={"validation": {"protocol": "group_loo"}})
        X = pd.DataFrame(np.arange(44).reshape(11, 4), dtype=float)
        y = np.arange(11, dtype=float) + 10
        groups = np.r_[np.arange(10), 0]
        class Worker:
            counts = []
            def fit(self, candidate, seed, specs, train, targets, validation):
                a = set(groups[train.index]); b = set(groups[validation.index])
                if a & b: raise AssertionError("duplicate group leaked")
                self.counts.append(len(a))
                return np.full(len(validation), targets.mean()), None
        worker = Worker()
        with tempfile.TemporaryDirectory(dir=str(ROOT / "outputs")) as temporary:
            directory = Path(temporary)
            self.assertIn((ROOT / "outputs").resolve(), directory.resolve().parents)
            bank, _ = _oof(X, y, groups, self.specs, {"model": "ridge"}, [101], cfg, worker, directory)
            self.assertEqual(bank.shape, (1, 1, 11))
            self.assertEqual(worker.counts, [9] * 10)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(AttributeScoreTests))
    (ROOT / "logs" / "tabpfn_attribute_scores_test_report.json").write_text(json.dumps({"status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
