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
        keys = required_release_keys({'delivery_profile': {'id': 'recommendation'}})
        self.assertIn('effectiveness_model', keys)
        self.assertIn('business_package', keys)


if __name__ == '__main__':
    unittest.main()
