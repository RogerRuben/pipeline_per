# -*- coding: utf-8 -*-
import unittest

from product.delivery_profile import data_token, normalize, request_allowed, required_release_keys


class DeliveryProfileTest(unittest.TestCase):
    def test_full_keeps_historical_data_token_and_pages(self):
        self.assertEqual(data_token('CLIENT_PRODUCT', 'full'), 'CLIENT_PRODUCT')
        self.assertNotEqual(data_token('CLIENT_PRODUCT', 'price_only'), data_token('CLIENT_PRODUCT', 'full'))
        self.assertNotEqual(data_token('CLIENT_PRODUCT', 'price_only'), data_token('CLIENT_PRODUCT', 'recommendation'))
        self.assertTrue(request_allowed('full', '/effectiveness'))
        self.assertTrue(request_allowed('full', '/api/evaluate'))
        self.assertEqual(required_release_keys({}), ('price_model', 'effectiveness_model', 'business_package', 'evaluation_metadata'))

    def test_price_package_blocks_other_products_and_skips_effectiveness_file(self):
        self.assertEqual(normalize('2'), 'price_only')
        self.assertTrue(request_allowed('price_only', '/price'))
        self.assertTrue(request_allowed('price_only', '/model-library'))
        self.assertTrue(request_allowed('price_only', '/api/price-workbench/predict'))
        self.assertFalse(request_allowed('price_only', '/'))
        self.assertFalse(request_allowed('price_only', '/effectiveness'))
        self.assertFalse(request_allowed('price_only', '/recommendation'))
        self.assertFalse(request_allowed('price_only', '/price/deep'))
        self.assertFalse(request_allowed('price_only', '/api/evaluate'))
        self.assertFalse(request_allowed('price_only', '/api/recommend'))
        self.assertFalse(request_allowed('price_only', '/api/generation/request'))
        self.assertFalse(request_allowed('price_only', '/api/effectiveness-workbench/evaluate'))
        self.assertFalse(request_allowed('price_only', '/api/deep-price/v1/predict'))
        self.assertTrue(request_allowed('price_only', '/api/price-workbench/predict'))
        self.assertTrue(request_allowed('price_only', '/login.js'))
        self.assertTrue(request_allowed('price_only', '/admin.js'))
        self.assertTrue(request_allowed('price_only', '/api/admin/snapshot'))
        self.assertTrue(request_allowed('price_only', '/favicon.png'))
        keys = required_release_keys({'delivery_profile': {'id': 'price_only'}})
        self.assertNotIn('effectiveness_model', keys)
        self.assertIn('price_model', keys)

    def test_recommendation_package_keeps_calculation_and_hides_other_pages(self):
        self.assertEqual(normalize('3'), 'recommendation')
        self.assertTrue(request_allowed('recommendation', '/'))
        self.assertTrue(request_allowed('recommendation', '/api/evaluate'))
        self.assertFalse(request_allowed('recommendation', '/price'))
        self.assertFalse(request_allowed('recommendation', '/price/deep'))
        self.assertFalse(request_allowed('recommendation', '/effectiveness'))
        self.assertFalse(request_allowed('recommendation', '/portal'))
        self.assertFalse(request_allowed('recommendation', '/api/price-workbench/schema'))
        self.assertFalse(request_allowed('recommendation', '/api/effectiveness-workbench/evaluate'))
        self.assertFalse(request_allowed('recommendation', '/api/deep-price/v1/schema'))
        self.assertTrue(request_allowed('recommendation', '/api/recommend'))
        self.assertTrue(request_allowed('recommendation', '/api/generation/request'))
        self.assertTrue(request_allowed('recommendation', '/api/bootstrap'))
        self.assertTrue(request_allowed('recommendation', '/app.js'))
        self.assertTrue(request_allowed('recommendation', '/admin.js'))
        self.assertFalse(request_allowed('recommendation', '/api/price-workbench/predict'))
        keys = required_release_keys({'delivery_profile': {'id': 'recommendation'}})
        self.assertIn('effectiveness_model', keys)
        self.assertIn('business_package', keys)

    def test_binary_assets_are_not_decoded(self):
        from product.delivery_profile import adjust_page
        png = b'\x89PNG\r\n\x1a\n' + bytes(range(32))
        self.assertEqual(adjust_page('price_only', 'favicon.png', png), png)
        page = adjust_page('price_only', 'price.html', '<a class="button secondary" href="/portal" target="_top">系统导航</a><a class="button secondary" href="/effectiveness">简易效能评价</a><a class="button secondary" href="/recommendation">智能推荐</a>'.encode('utf-8'))
        self.assertIn('模型库'.encode('utf-8'), page)
        self.assertNotIn(b'/effectiveness', page)

    def test_other_profiles_ignore_legacy_user_database(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from product.common import data_directory
        root = Path(tempfile.mkdtemp())
        local = root / 'local'
        legacy = local / 'PipelinePer' / 'data' / 'protocol_demo.db'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'not a database')
        package = root / 'pkg'
        package.mkdir()
        previous = os.environ.get('LOCALAPPDATA')
        os.environ['LOCALAPPDATA'] = str(local)
        os.environ.pop('PIPELINE_PER_DATA_DIR', None)
        try:
            for profile in ('price_only', 'recommendation'):
                (package / 'PACKAGE_MANIFEST.json').write_text(json.dumps({
                    'release': {'product_code': 'CLIENT_FILE'},
                    'delivery_profile': {'id': profile},
                }), encoding='utf-8')
                with patch('product.compatibility.inspect_database', side_effect=AssertionError('legacy inspected')):
                    target = data_directory(package)
                self.assertNotEqual(target.resolve(), (local / 'PipelinePer').resolve())
                self.assertIn('products', target.parts)
            (package / 'PACKAGE_MANIFEST.json').write_text(json.dumps({
                'release': {'product_code': 'CLIENT_FILE'},
                'delivery_profile': {'id': 'full'},
            }), encoding='utf-8')
            with patch('product.compatibility.inspect_database', return_value={'product_code': 'CLIENT_FILE'}):
                reused = data_directory(package)
            self.assertEqual(reused.resolve(), (local / 'PipelinePer').resolve())
        finally:
            if previous is None:
                os.environ.pop('LOCALAPPDATA', None)
            else:
                os.environ['LOCALAPPDATA'] = previous

    def test_price_package_does_not_watch_effectiveness(self):
        import inspect
        from product.supervisor import Supervisor
        ghost = type('Box', (), {'manifest': {'delivery_profile': {'id': 'price_only'}}})()
        roles = Supervisor.active_roles(ghost)
        self.assertEqual(roles, ('price', 'web'))
        self.assertNotIn('effectiveness', roles)
        source = inspect.getsource(Supervisor.run)
        self.assertIn('for role in self.active_roles()', source)
        self.assertNotIn('for role in ROLES', source)

    def test_price_assemble_skips_effectiveness_generator(self):
        import inspect
        from tools.package_existing_model import assemble
        source = inspect.getsource(assemble)
        self.assertIn("if profile != 'price_only':", source)
        self.assertLess(source.index("if profile != 'price_only':"), source.index('build_effectiveness'))
        self.assertIn("if profile != 'price_only':", source[source.index('build_effectiveness'):])


if __name__ == '__main__':
    unittest.main()
