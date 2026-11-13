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

    def test_undefined_r2_is_rejected_by_the_search_objective(self):
        from services.price_service.optuna_v2.cached_backend import CachedBackend
        from services.price_service.optuna_v2.price_automl_core import search_objectives
        scores = {'r2': None, 'mape': 0.2, 'mae': 1.0, 'rmse': 1.0, 'r2_status': 'undefined'}
        with self.assertRaisesRegex(ValueError, '不可定义'):
            search_objectives(scores)
        backend = CachedBackend()
        with self.assertRaisesRegex(ValueError, '不记为 R²=0'):
            backend._check_metrics(scores, np.array([3.0, 3.0]), np.array([3.0, 3.1]), None, [0, 1], {})

    def test_static_override_rejects_a_sibling_directory(self):
        import tempfile
        from pathlib import Path
        from product.static_override import override_file
        folder = Path(tempfile.mkdtemp())
        (folder / 'page.html').write_text('新文字', encoding='utf-8')
        sibling = Path(str(folder) + '_extra')
        sibling.mkdir()
        (sibling / 'page.html').write_text('旁路', encoding='utf-8')
        self.assertEqual(override_file(folder, 'page.html').read_text(encoding='utf-8'), '新文字')
        self.assertIsNone(override_file(folder, '../' + sibling.name + '/page.html'))
        self.assertIsNone(override_file(folder, 'missing.py'))

    def test_zero_price_variance_does_not_report_r2_as_zero(self):
        metrics = regression_metrics([10, 10, 10], [10, 11, 9])
        self.assertIsNone(metrics['r2'])
        self.assertEqual(metrics['r2_status'], 'undefined')
        self.assertEqual(metrics['objective'], 'r2_and_mape')
        varied = regression_metrics([10, 20, 30], [10, 20, 30])
        self.assertEqual(varied['r2_status'], 'defined')
        self.assertAlmostEqual(varied['r2'], 1.0)
        small = regression_metrics([1e-8, 2e-8, 3e-8], [1e-8, 2e-8, 3e-8])
        self.assertEqual(small['r2_status'], 'defined')
        # A real zero is the mean predictor on varying prices. It is not the undefined case.
        zero = regression_metrics([10, 20, 30], [20, 20, 20])
        self.assertEqual(zero['r2_status'], 'defined')
        self.assertAlmostEqual(zero['r2'], 0.0)
        self.assertEqual(zero['recorded_only'], ['mae', 'rmse'])
        self.assertGreater(zero['mae'], 0)

    def test_builtin_hash_roundtrip_uses_normalized_newlines(self):
        key = 'pump/regional_context.py'
        if key not in NORM_HASHES:
            self.skipTest('pump runtime hash is not in this build')
        self.assertEqual(len(NORM_HASHES[key]), 64)
        self.assertNotEqual(hashlib.sha256(b'changed').hexdigest(), NORM_HASHES[key])


if __name__ == '__main__':
    unittest.main()
