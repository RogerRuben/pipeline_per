# -*- coding: utf-8 -*-
"""Regression cases for valid pasted headers, demo scores and inactive attributes."""
import argparse
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.product_table import clean_field_id,TableFormatError
from app.historical_onboarding import HistoricalProductOnboarding
from tools.product_price_tabpfn import load_config
from tools.product_r4_input import ratings_from_workbook,apply_field_types
from tools.product_price_automl import price_dataset
from tools.price_attribute_input import (DEMO_SCORES,resolve_price_expert,price_attribute_audit,
                                        resolve_field_mapping)
CASES=ROOT/'outputs/price_input_fix_20261015/workbooks'


def analyze(path):
    return HistoricalProductOnboarding().analyze(path.name,path.read_bytes(),'PASTE_TEST','复制粘贴测试',
                ['-1','\\','/'],require_dual_header=True)['data']


def prepare(path):
    data=analyze(path)
    config=load_config(ROOT/'docs/examples/price_tabpfn_micro_robust.config.json')
    scores,records,types,groups=ratings_from_workbook(path,{p['parameter_id'] for p in data['parameters']})
    expert,resolution=resolve_price_expert(config['expert'],data['parameters'],scores,records,types,groups)
    apply_field_types(data,expert['field_types'])
    X,y,specs,mappings,business_groups,ids=price_dataset(data,8)
    return data,expert,resolution,X,price_attribute_audit(data['parameters'],X.columns,expert,resolution)


class PriceInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original=ROOT/'tests/fixtures/product_pipeline_v2/virtual_10x50.xlsx'
        cls.data=analyze(cls.original)
        cls.config=ROOT/'docs/examples/price_tabpfn_micro_robust.config.json'

    def test_formatting_characters_and_width_do_not_change_valid_id(self):
        self.assertEqual(clean_field_id('\u2066\u200bＲＡＴＥＤ＿ＴＨＲＵＳＴ＿Ｎ\u2069\u00a0'),'RATED_THRUST_N')
        self.assertEqual(clean_field_id('a-b'),'a-b')

    def test_original_example_effective_scores_are_numerically_unchanged(self):
        cfg=load_config(self.config)
        expert,resolution=resolve_price_expert(cfg['expert'],self.data['parameters'])
        self.assertEqual(expert['scores'],DEMO_SCORES)
        self.assertFalse(resolution['ignored_example_scores'])

    def test_custom_json_is_strict_even_when_copied_from_example(self):
        raw=json.loads(self.config.read_text(encoding='utf-8'))
        with tempfile.TemporaryDirectory(dir=str(ROOT/'outputs')) as folder:
            custom=Path(folder)/'my_actual_expert.json'
            custom.write_text(json.dumps(raw),encoding='utf-8')
            cfg=load_config(custom)
            self.assertEqual(cfg['expert']['scores'],DEMO_SCORES)
            changed=copy.deepcopy(self.data['parameters'])
            for parameter in changed:
                if parameter['parameter_id']=='noise_db': parameter['parameter_id']='acoustic_noise_db'
            with self.assertRaises(TableFormatError) as caught:
                resolve_price_expert(cfg['expert'],changed)
            self.assertEqual(caught.exception.details['field_id'],'noise_db')
            self.assertIn('JSON expert.scores',str(caught.exception))

    def test_explicit_score_override_takes_priority_over_demo(self):
        cfg=load_config(self.config,{'expert':{'scores':{'rated_thrust_n':4}}})
        expert,_=resolve_price_expert(cfg['expert'],self.data['parameters'])
        self.assertEqual(expert['scores']['rated_thrust_n'],4)

    def test_case_and_copy_formatting_are_resolved_without_renaming_data(self):
        values,changes,_=resolve_field_mapping({'\ufeffRATED_THRUST_N\u2069':5},{'rated_thrust_n'},'JSON expert.scores')
        self.assertEqual(values,{'rated_thrust_n':5})
        self.assertEqual(changes[0]['field_id'],'rated_thrust_n')

    def test_conflicting_aliases_cannot_silently_override_each_other(self):
        with self.assertRaises(TableFormatError):
            resolve_field_mapping({'rated_thrust_n':5,'RATED_THRUST_N':3},{'rated_thrust_n'},'JSON expert.scores')

    def test_genuine_unknown_explicit_id_has_source_and_suggestion(self):
        with self.assertRaises(TableFormatError) as caught:
            resolve_field_mapping({'rated_thurts_n':5},{'rated_thrust_n'},'JSON expert.scores')
        self.assertEqual(caught.exception.details['suggestions'],['rated_thrust_n'])

    def test_valid_scored_blank_column_is_not_called_an_unknown_id(self):
        data,expert,resolution,X,rows=prepare(CASES/'copied_blank_column.xlsx')
        self.assertEqual(X.shape,(10,49))
        row=next(row for row in rows if row['field_id']=='sample_rate_hz')
        self.assertTrue(row['specified']);self.assertFalse(row['used_in_training'])
        self.assertIn('整列为空',row['exclusion_reason'])
        self.assertEqual(len(rows),50)

    def test_valid_replacement_id_is_not_blocked_by_leftover_demo_scores(self):
        data,expert,resolution,X,rows=prepare(CASES/'copied_renamed_field.xlsx')
        self.assertEqual(X.shape,(10,50))
        self.assertNotIn('noise_db',expert['scores'])
        self.assertIn('acoustic_noise_db',X.columns)
        row=next(row for row in rows if row['field_id']=='acoustic_noise_db')
        self.assertEqual(row['score'],3);self.assertFalse(row['specified'])
        self.assertEqual(resolution['ignored_example_scores'][0]['field_id'],'noise_db')

    def test_reordered_chinese_headers_and_uppercase_expert_ids(self):
        data,expert,resolution,X,rows=prepare(CASES/'copied_combined.xlsx')
        self.assertEqual(expert['scores']['rated_thrust_n'],5)
        self.assertEqual(len(expert['records']),3)
        self.assertEqual(expert['records'][0]['source_cell'],'C2')
        self.assertEqual(expert['records'][0]['field_id'],'rated_thrust_n')
        self.assertEqual(next(r for r in rows if r['field_id']=='rated_thrust_n')['score_source'],'workbook')
        self.assertEqual(X.shape,(10,49))

    def test_real_mistype_in_workbook_points_to_actual_code_column(self):
        path=CASES/'genuine_unknown_rating.xlsx'
        known={p['parameter_id'] for p in analyze(path)['parameters']}
        with self.assertRaises(TableFormatError) as caught: ratings_from_workbook(path,known)
        self.assertEqual(caught.exception.details['sheet_name'],'价格属性评分')
        self.assertEqual(caught.exception.details['error_cell'],'B2')
        self.assertIn('rated_thrust_n',caught.exception.details['suggestions'])

    def test_fields_observed_only_on_unpriced_rows_are_not_training_features(self):
        data=copy.deepcopy(self.data)
        for i,row in enumerate(data['agreements']):
            if i==0: row['historical_price_wan']=None
            else: row['params'].pop('sample_rate_hz',None)
        X,*_=price_dataset(data,8)
        self.assertEqual(X.shape,(9,49))
        self.assertNotIn('sample_rate_hz',X.columns)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--cases',type=Path,default=CASES)
    args=p.parse_args();CASES=args.cases.resolve()
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(PriceInputTests))
    (ROOT/'logs').mkdir(exist_ok=True)
    (ROOT/'logs/price_attribute_input_test_report.json').write_text(json.dumps({'status':'PASS' if result.wasSuccessful() else 'FAIL',
        'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)},indent=2),encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)