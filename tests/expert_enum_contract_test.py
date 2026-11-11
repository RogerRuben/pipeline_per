"""Expert enum domains must survive both conversion and delivery validation."""
import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from product.compatibility import effectiveness_schema_from_document, make_contract

FORMATS = [('model_format', 'lock-product-reuse-model-17.0', 'schema'),
           ('model_format_version', 'system-effectiveness-frozen-model-13.0', 'product_schema'),
           ('model_format_version', 'effectiveness-frozen-model-15.0', 'project_schema')]


def document(fmt, domain=None):
    attribute = {'key': 'connector', 'data_type': 'categorical', 'unit': ''}
    attribute.update(domain or {})
    return {fmt[0]: fmt[1], fmt[2]: {'attributes': [attribute]}}


def contract(raw, values, mapping=None):
    schema = {'model_version': 'price-test', 'feature_order': ['length'],
              'fields': [{'field_name': 'length', 'dtype': 'number', 'required': True}]}
    business = {'product_code': 'SYNTHETIC', 'source_release_id': 'fixture', 'payload_sha256': 'fixture',
                'data': {'parameters': [
                    {'parameter_id': 'length', 'value_type': 'number', 'required': True},
                    {'parameter_id': 'connector', 'value_type': 'enum', 'required': True,
                     'allowed_values': values, 'model_value_mapping': mapping or {}}]}}
    return make_contract(schema, business, 'price-hash', {'unit': 'wan_yuan', 'scale': 1},
                         effectiveness_schema=raw, effectiveness_model_sha='effect-hash')


class ExpertEnumTests(unittest.TestCase):
    def test_allowed_values_survive_all_three_expert_formats(self):
        for fmt in FORMATS:
            with self.subTest(fmt=fmt[1]):
                raw = document(fmt, {'allowed_values': ['A', 'B']})
                field = contract(raw, ['A', 'B'])['effectiveness_schema']['fields'][0]
                self.assertEqual(field['dtype'], 'enum')
                self.assertEqual(field['allowed_values'], ['A', 'B'])
                self.assertEqual(field['missing_policy'], 'reject')

    def test_categories_are_a_model_domain(self):
        for fmt in FORMATS:
            raw = document(fmt, {'categories': ['A', 'B']})
            self.assertEqual(contract(raw, ['B'])['effectiveness_schema']['fields'][0]['allowed_values'], ['A', 'B'])

    def test_zero_and_false_enum_members_are_not_dropped(self):
        for fmt in FORMATS:
            raw = document(fmt, {'allowed_values': [0, False, 2]})
            self.assertEqual(contract(raw, [0, False, 2])['effectiveness_schema']['fields'][0]['allowed_values'], [0, False, 2])

    def test_declared_category_mapping_preserved(self):
        for fmt in FORMATS:
            raw = document(fmt, {'category_mapping': {'A': 1, 'B': 2}})
            self.assertEqual(contract(raw, ['A', 'B'])['effectiveness_schema']['fields'][0]['category_mapping'], {'A': 1, 'B': 2})

    def test_business_to_model_encoding_must_still_match(self):
        for fmt in FORMATS:
            raw = document(fmt, {'allowed_values': [1, 2]})
            contract(raw, ['甲', '乙'], {'甲': 1, '乙': 2})
            with self.assertRaisesRegex(ValueError, 'connector'):
                contract(raw, ['甲', '乙'], {'甲': 1, '乙': 3})

    def test_undeclared_business_value_is_rejected(self):
        for fmt in FORMATS:
            with self.assertRaisesRegex(ValueError, 'connector:C'):
                contract(document(fmt, {'allowed_values': ['A', 'B']}), ['A', 'C'])

    def test_missing_or_empty_model_domain_is_not_inferred(self):
        for fmt in FORMATS:
            for domain in ({}, {'allowed_values': []}):
                raw = document(fmt, domain)
                raw['baseline'] = {'params': {'connector': 'A'}}
                with self.assertRaisesRegex(ValueError, 'connector:A'):
                    contract(raw, ['A'])

    def test_schema_field_domain_is_joined_by_english_id(self):
        for fmt in FORMATS:
            raw = document(fmt)
            raw[fmt[2]]['fields'] = [{'field_name': 'different', 'allowed_values': ['bad']},
                                     {'field_name': 'connector', 'allowed_values': ['A', 'B']}]
            self.assertEqual(contract(raw, ['A', 'B'])['effectiveness_schema']['fields'][0]['allowed_values'], ['A', 'B'])

    def test_conversion_does_not_mutate_model(self):
        for fmt in FORMATS:
            raw = document(fmt, {'categories': ['A', 'B']})
            before = copy.deepcopy(raw)
            effectiveness_schema_from_document(raw)
            self.assertEqual(raw, before)

    def test_duplicate_attributes_and_domain_ids_are_rejected(self):
        for fmt in FORMATS:
            raw = document(fmt)
            raw[fmt[2]]['attributes'] *= 2
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                effectiveness_schema_from_document(raw)
            raw = document(fmt)
            raw[fmt[2]]['fields'] = [{'field_name': 'connector'}] * 2
            with self.assertRaisesRegex(ValueError, 'duplicate'):
                effectiveness_schema_from_document(raw)


if __name__ == '__main__':
    unittest.main(verbosity=2)
