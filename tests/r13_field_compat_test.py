# -*- coding: utf-8 -*-
import hashlib
import unittest

from services.effectiveness_service.expert_packages import source_difference
from services.effectiveness_service.expert_runtime.source_hashes import NORM_HASHES
from services.price_service.optuna_v2.price_automl_core import regression_metrics
import numpy as np


class R13CompatTests(unittest.TestCase):
    def test_source_hash_difference_is_recorded_not_executed(self):
        key = next(iter(NORM_HASHES))
        family, name = key.split('/', 1)
        matched = source_difference(family, 'source/' + name, b'unused')
        self.assertIn(matched['consistency'], ('verified', 'unverified'))
        changed = source_difference(family, 'source/' + name, b'not-the-builtin-source\n')
        self.assertEqual(changed['consistency'], 'unverified')
        self.assertEqual(changed['status'], 'source_differs_from_builtin')

    def test_zero_price_variance_does_not_report_r2_as_zero(self):
        metrics = regression_metrics([10, 10, 10], [10, 11, 9])
        self.assertIsNone(metrics['r2'])
        self.assertEqual(metrics['r2_status'], 'undefined')
        self.assertEqual(metrics['objective'], 'r2_and_mape')
        varied = regression_metrics([10, 20, 30], [10, 20, 30])
        self.assertEqual(varied['r2_status'], 'defined')
        self.assertAlmostEqual(varied['r2'], 1.0)

    def test_builtin_hash_roundtrip_uses_normalized_newlines(self):
        key = 'pump/regional_context.py'
        if key not in NORM_HASHES:
            self.skipTest('pump runtime hash is not in this build')
        self.assertEqual(len(NORM_HASHES[key]), 64)
        self.assertNotEqual(hashlib.sha256(b'changed').hexdigest(), NORM_HASHES[key])


if __name__ == '__main__':
    unittest.main()
