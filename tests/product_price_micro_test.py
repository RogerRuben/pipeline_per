# -*- coding: utf-8 -*-
"""Micro-sample dimensionality, cache and expert constraint regressions."""
from __future__ import print_function
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
from tools.product_price_automl_v2 import load_config, estimator, _fusion
from tools.product_price_micro import portfolio, micro_oof, validation_metrics, stabilize_weights
from services.price_service.ensemble_estimators import StableFoldImputer, FoldwiseProjection, EffectiveDFRidge, EngineeringPriceRegressor, EngineeringCalibratedRegressor
from tools.benchmark_product_micro import scenario
from tools.product_price_automl import price_dataset


class MicroTests(unittest.TestCase):
    def test_fold_empty_columns_are_preserved_without_validation_leakage(self):
        imputer = StableFoldImputer(add_indicator=True).fit([[1., np.nan], [3., np.nan]])
        np.testing.assert_array_equal(imputer.statistics_, [2., 0.])
        self.assertEqual(imputer.transform([[np.nan, 999.]]).shape, (1, 3))
        np.testing.assert_array_equal(imputer.statistics_, [2., 0.])

    def test_projection_is_unsupervised_and_constant_safe(self):
        X = np.random.RandomState(11).normal(size=(6, 40))
        a = FoldwiseProjection(2).fit(X, np.arange(6))
        b = FoldwiseProjection(2).fit(X, np.arange(6)[::-1])
        np.testing.assert_allclose(a.pca_.components_, b.pca_.components_)
        previous = a.pca_.mean_.copy(); a.transform(np.full((2, 40), 1e8))
        np.testing.assert_array_equal(previous, a.pca_.mean_)
        self.assertEqual(FoldwiseProjection(2).fit(np.ones((4, 40))).transform(np.ones((1, 40))).shape, (1, 1))

    def test_high_dimensional_ridge_caps_linear_degrees_of_freedom(self):
        X = np.random.RandomState(31).normal(size=(6, 40))
        model = EffectiveDFRidge(alpha=1., max_effective_df=3.).fit(X, np.linspace(5, 9, 6))
        self.assertLessEqual(model.effective_df_, 3. + 1e-8)
        self.assertGreaterEqual(model.fitted_alpha_, 40.)
        self.assertTrue(np.all(np.isfinite(model.predict(X))))

    def test_engineering_correction_is_anchored_and_has_two_parameters(self):
        X = np.arange(8, dtype=float).reshape(-1, 1)
        model = EngineeringCalibratedRegressor(EngineeringPriceRegressor(["x"], {"x": 1.}, 10.), strength=8.).fit(X, 40 - X[:, 0] * 10)
        self.assertEqual(model.correction_.shape, (2,))
        self.assertLessEqual(abs(model.correction_[1] / model.base_scale_), .5)
        self.assertTrue(np.all(model.predict(X) > 0))

    def test_portfolio_has_no_supervised_selector_and_only_depth_one_trees(self):
        data, cfg, _, _ = scenario("expert_cost", 41)
        config = load_config(overrides=cfg)
        _, _, specs, _, _, _ = price_dataset(data, 8)
        candidates = portfolio(specs, config)
        self.assertLessEqual(len(candidates), 13)
        for row in candidates:
            candidate = row["config"]
            if candidate["model"] in ("extra_trees", "random_forest", "gbdt"):
                self.assertEqual(candidate["max_depth"], 1)
                self.assertGreaterEqual(candidate["min_samples_leaf"], 2)
            if "pipeline" in candidate:
                fitted = estimator(candidate, 101, specs, config)
                pipeline = getattr(fitted, "regressor", fitted)
                self.assertIsInstance(pipeline.named_steps["select"], FoldwiseProjection)

    def test_single_group_r2_is_not_invented(self):
        self.assertIsNone(validation_metrics([10.], [9.])["r2"])
        self.assertIsNone(validation_metrics([10., 12.], [9., 11.], single_group=True)["r2"])
        self.assertIsNotNone(validation_metrics([10., 12.], [9., 11.])["r2"])

    def test_loo_cache_tracks_df_settings_and_never_mixes_rows(self):
        data, cfg, _, _ = scenario("noise", 41)
        config = load_config(overrides=cfg)
        frame, y, specs, _, groups, _ = price_dataset(data, 8)
        calls, owner = [], self
        class Spy(object):
            def fit(self, candidate, seed, specs, train, y, validation):
                calls.append(len(train)); owner.assertFalse(set(train.index) & set(validation.index))
                return np.full(len(validation), np.mean(y)), None
        (ROOT / "outputs").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="micro_cache_", dir=str(ROOT / "outputs")) as directory:
            self.assertIn((ROOT / "outputs").resolve(), Path(directory).resolve().parents)
            worker, candidate = Spy(), {"model": "baseline"}
            _, hit = micro_oof(frame, y, groups, specs, candidate, [101], config, worker, Path(directory))
            self.assertFalse(hit); self.assertEqual(calls, [9] * 10)
            _, hit = micro_oof(frame, y, groups, specs, candidate, [101], config, worker, Path(directory))
            self.assertTrue(hit); self.assertEqual(len(calls), 10)
            amended = copy.deepcopy(config); amended["small_sample"]["max_effective_df"] = 2.
            _, hit = micro_oof(frame, y, groups, specs, candidate, [101], amended, worker, Path(directory))
            self.assertFalse(hit); self.assertEqual(len(calls), 20)

    def test_jackknife_retains_original_budget_and_explanation_weights(self):
        y = np.linspace(8., 20., 10)
        bank = np.column_stack([np.full(10, 12.), y * .99, y * 1.02])
        candidates = [{"config": {"model": "baseline"}}, {"config": {"model": "ridge"}}, {"config": {"model": "svr"}}]
        config = load_config(overrides={"ensemble": {"mode": "performance_budget", "family_priors": [
            {"family": "linear", "weight": .6, "confidence": .8, "lower_bound": .2, "upper_bound": .9},
            {"family": "kernel", "weight": .4, "confidence": .6}]}})
        result = stabilize_weights(y, bank, np.arange(10), candidates, config, _fusion(y, bank, candidates, config))
        self.assertAlmostEqual(sum(result["weights"]), 1, places=7)
        self.assertLessEqual(result["mape_delta_percentage_points"], .1 + 1e-6)
        self.assertLessEqual(result["r2_drop"], .005 + 1e-8)
        for row in result["expert_groups"]:
            self.assertAlmostEqual(row["learned_weight"], sum(result["weights"][i] for i in row["members"]), places=7)

    def test_invalid_micro_controls_fail(self):
        for value in ({"policy": "magic"}, {"max_effective_df": 0}, {"tree_seeds": 4.5}, {"ridge_alphas": [0]}, {"log_target": "yes"}):
            with self.assertRaises(ValueError):
                load_config(overrides={"small_sample": value})


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(MicroTests))
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs/product_price_micro_test_report.json").write_text(json.dumps({"status": "PASS" if result.wasSuccessful() else "FAIL",
        "tests": result.testsRun, "failures": len(result.failures), "errors": len(result.errors)}, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
