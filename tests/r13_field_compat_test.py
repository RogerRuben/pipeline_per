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

    def test_packaging_does_not_rewrite_a_mismatched_model_manifest(self):
        import tempfile
        from pathlib import Path
        from tools.package_existing_model import check_input_contract, verify_sidecar
        folder = Path(tempfile.mkdtemp())
        bundle = folder / 'price_native_bundle.pkl'
        sidecar = folder / 'price_native_bundle.pkl.manifest.json'
        bundle.write_bytes(b'model-bytes')
        sidecar.write_text('{"bundle_sha256": "%s", "bundle_file": "price_native_bundle.pkl"}\n' % ('0' * 64), encoding='utf-8')
        original = sidecar.read_bytes()
        with self.assertRaisesRegex(ValueError, '不会改写清单'):
            verify_sidecar(sidecar, bundle)
        self.assertEqual(sidecar.read_bytes(), original)
        digest = hashlib.sha256(bundle.read_bytes()).hexdigest()
        sidecar.write_text('{"bundle_sha256": "%s"}\n' % digest, encoding='utf-8')
        before = sidecar.read_bytes()
        self.assertEqual(verify_sidecar(sidecar, bundle), digest)
        self.assertEqual(sidecar.read_bytes(), before)
        saved = {'feature_order': ['grade', 'thrust_n'], 'feature_schema': [
            {'field_name': 'grade', 'dtype': 'enum', 'category_mapping': {'甲': 0, '乙': 1}},
            {'field_name': 'thrust_n', 'dtype': 'number', 'category_mapping': None}]}
        data = {'parameters': [{'parameter_id': 'grade', 'value_type': 'enum'}, {'parameter_id': 'thrust_n', 'value_type': 'number'}],
                'agreements': [{'params': {'grade': '甲', 'thrust_n': 10}}]}
        self.assertEqual(check_input_contract(['grade', 'price_wan', 'thrust_n'], data, saved), ['grade', 'thrust_n'])
        with self.assertRaisesRegex(ValueError, '顺序'):
            check_input_contract(['thrust_n', 'grade'], data, saved)
        with self.assertRaisesRegex(ValueError, '类型'):
            check_input_contract(['grade', 'thrust_n'], dict(data, parameters=[
                {'parameter_id': 'grade', 'value_type': 'number'}, {'parameter_id': 'thrust_n', 'value_type': 'number'}]), saved)
        with self.assertRaisesRegex(ValueError, '编码'):
            check_input_contract(['grade', 'thrust_n'], dict(data, agreements=[{'params': {'grade': '丙', 'thrust_n': 10}}]), saved)

    def test_input_parsing_uses_the_supplied_field_type(self):
        import tempfile
        from pathlib import Path
        from tools.train_price_only import load_input_table
        folder = Path(tempfile.mkdtemp())
        table = folder / 'products.csv'
        table.write_bytes('推力,等级,价格(万元)\r\nthrust_n,grade,price_wan\r\n10,1,20\r\n11,2,21\r\n12,1,22\r\n13,2,23\r\n'.encode('utf-8-sig'))
        config = folder / 'types.json'
        config.write_text('{"expert": {"field_types": {"grade": "enum"}}}', encoding='utf-8')
        plain = load_input_table(table, 'optuna_v3', 'R13_TYPE', '类型检查')
        typed = load_input_table(table, 'optuna_v3', 'R13_TYPE', '类型检查', str(config))
        kinds = {item['parameter_id']: item['value_type'] for item in plain['parameters']}
        declared = {item['parameter_id']: item['value_type'] for item in typed['parameters']}
        self.assertEqual(kinds['grade'], 'number')
        self.assertEqual(declared['grade'], 'enum')
        self.assertEqual(typed['agreements'][0]['params']['grade'], '1')

    def test_builtin_hash_roundtrip_uses_normalized_newlines(self):
        key = 'pump/regional_context.py'
        if key not in NORM_HASHES:
            self.skipTest('pump runtime hash is not in this build')
        self.assertEqual(len(NORM_HASHES[key]), 64)
        self.assertNotEqual(hashlib.sha256(b'changed').hexdigest(), NORM_HASHES[key])


if __name__ == '__main__':
    unittest.main()
