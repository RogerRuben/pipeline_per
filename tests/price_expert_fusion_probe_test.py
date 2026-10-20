# -*- coding: utf-8 -*-
"""Exercise expert/data conflicts rather than mirroring the optimizer internals."""
from __future__ import print_function

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import numpy as np
from tools.price_expert_fusion_probe import FusionConstraintError, fit_expert_weights


class ExpertFusionTests(unittest.TestCase):
    def setUp(self):
        self.y = np.linspace(2.0, 10.0, 24)

    def assertLegalWeights(self, result):
        self.assertGreaterEqual(min(result["weights"]), -1e-7)
        self.assertAlmostEqual(sum(result["weights"]), 1.0, places=7)

    def test_poor_member_cannot_override_error_budget_with_large_expert_prior(self):
        bank = np.column_stack([self.y, self.y * 1.01, self.y * 1.5])
        result = fit_expert_weights(self.y, bank, [.1, .1, .8], max_mape_delta_pp=.2, max_r2_drop=.001)
        self.assertLegalWeights(result)
        self.assertLess(result["weights"][2], .005)
        self.assertLessEqual(result["mape_delta_percentage_points"], .2 + 1e-6)
        self.assertLessEqual(result["r2_drop"], .001 + 1e-8)

    def test_expert_prior_is_preserved_when_accuracy_budget_allows_it(self):
        bank = np.column_stack([self.y * .99, self.y * 1.01])
        result = fit_expert_weights(self.y, bank, [.8, .2], max_mape_delta_pp=1, max_r2_drop=.01)
        self.assertLegalWeights(result)
        np.testing.assert_allclose(result["weights"], [.8, .2], atol=2e-5)

    def test_soft_zero_and_unspecified_prior_do_not_ban_accurate_members(self):
        bank = np.column_stack([self.y, self.y * 1.5, self.y * 1.25])
        result = fit_expert_weights(self.y, bank, [0, .8, None], max_mape_delta_pp=.01)
        self.assertGreater(result["weights"][0], .999)
        self.assertIsNone(result["expert_weight_deltas"][2])

    def test_invalid_predictions_are_banned_but_mandatory_invalid_member_conflicts(self):
        bank = np.column_stack([self.y, self.y * 1.1, self.y.copy()])
        bank[3, 2] = np.nan
        result = fit_expert_weights(self.y, bank, [.1, .1, .8], max_mape_delta_pp=.1)
        self.assertEqual(result["invalid_model_indices"], [2])
        self.assertAlmostEqual(result["weights"][2], 0)
        self.assertTrue(np.isfinite(result["development_metrics"]["mape_percent"]))
        with self.assertRaises(FusionConstraintError):
            fit_expert_weights(self.y, bank, [.1, .1, .8], lower_bounds=[0, 0, .1])

    def test_hard_constraints_and_error_budget_conflict_is_explicit(self):
        bank = np.column_stack([self.y, self.y * 1.5])
        with self.assertRaises(FusionConstraintError):
            fit_expert_weights(self.y, bank, [.2, .8], lower_bounds=[.2, .8], upper_bounds=[.2, .8],
                               max_mape_delta_pp=.1)
        with self.assertRaises(FusionConstraintError):
            fit_expert_weights(self.y, bank, [.2, .8], lower_bounds=[.6, .6])

    def test_shared_bias_does_not_become_an_accuracy_claim_after_fusion(self):
        bank = np.column_stack([np.full(len(self.y), .5), np.full(len(self.y), .8)])
        result = fit_expert_weights(self.y, bank, mode="data_only")
        self.assertLegalWeights(result)
        self.assertGreater(result["development_metrics"]["mape_percent"], 75)
        self.assertFalse(result["beats_constant_mape_baseline"])

    def test_soft_prior_strength_changes_trust_without_forcing_fixed_weights(self):
        bank = np.column_stack([self.y, self.y * 1.5])
        weak = fit_expert_weights(self.y, bank, [.1, .9], mode="soft_prior", prior_strength=0)
        strong = fit_expert_weights(self.y, bank, [.1, .9], mode="soft_prior", prior_strength=10)
        self.assertLess(weak["weights"][1], 1e-5)
        self.assertGreater(strong["weights"][1], weak["weights"][1] + .1)
        self.assertLess(strong["weights"][1], .9)

    def test_invalid_targets_and_mislabelled_percentage_prior_are_rejected(self):
        bank = np.column_stack([self.y, self.y * 1.01])
        with self.assertRaises(ValueError):
            fit_expert_weights(self.y, bank, [80, 20])
        with self.assertRaises(ValueError):
            fit_expert_weights(np.ones(len(self.y)), bank, [.8, .2])

    def test_lower_expert_confidence_reduces_pull_at_same_regularization(self):
        bank = np.column_stack([self.y, self.y * 1.5])
        low = fit_expert_weights(self.y, bank, [.1, .9], confidence=[.5, .5], mode="soft_prior", prior_strength=1)
        high = fit_expert_weights(self.y, bank, [.1, .9], confidence=[1, 1], mode="soft_prior", prior_strength=1)
        self.assertLess(low["weights"][1], high["weights"][1] - .1)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(ExpertFusionTests))
    report = {"status": "PASS" if result.wasSuccessful() else "FAIL", "tests": result.testsRun,
              "failures": len(result.failures), "errors": len(result.errors)}
    (ROOT / "logs").mkdir(exist_ok=True)
    (ROOT / "logs" / "price_expert_fusion_probe_test_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    sys.exit(0 if result.wasSuccessful() else 1)
