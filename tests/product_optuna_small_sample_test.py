# -*- coding: utf-8 -*-
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
from threadpoolctl import threadpool_limits
from product_optuna_v3_test import mixed_fixture
from tools.product_price_optuna_v2 import load_config,dataset,training_split,train_price_optuna_v2
from tools.product_price_optuna_v3 import load_config as v3_config
from services.price_service.native_bundle import load_bundle,predict_batch


class SmallSampleTests(unittest.TestCase):
    def test_minimum_is_configurable_and_safeguards_are_explicit(self):
        self.assertEqual(load_config()['input']['min_samples'],4);self.assertEqual(v3_config()['input']['min_samples'],4)
        cfg=load_config(overrides={'input':{'min_samples':6}})
        with self.assertRaisesRegex(ValueError,'配置的6条'):dataset(mixed_fixture(5,40),cfg)
        for value in (0,1,3,True,4.5):
            with self.assertRaises(ValueError):load_config(overrides={'input':{'min_samples':value}})
        for records in (1,2,3):
            with self.assertRaisesRegex(ValueError,'少于'):training_split(records,load_config())
    def test_automatic_split_supports_four_to_seven_without_one_row_r2(self):
        for records,expected_train in ((4,2),(5,3),(6,4),(7,5)):
            with self.subTest(records=records):
                ratio,report=training_split(records,load_config());self.assertEqual(report['validation_rows'],2)
                self.assertEqual(report['training_rows'],expected_train);self.assertEqual(int(np.ceil(ratio*records)),2)
                self.assertEqual(report['adjusted'],records<6)
        ratio,report=training_split(16,load_config());self.assertEqual(ratio,.2);self.assertFalse(report['adjusted'])
        self.assertEqual(report['training_rows'],12);self.assertEqual(report['validation_rows'],4)
    def test_strict_split_rejects_impossible_ratios_and_auto_mode_handles_high_ratio(self):
        cfg=load_config(overrides={'validation':{'auto_adjust_test_size':False}})
        with self.assertRaisesRegex(ValueError,'至少2条'):training_split(4,cfg)
        cfg=load_config(overrides={'validation':{'test_size':.79}})
        ratio,report=training_split(4,cfg);self.assertEqual(ratio,.5);self.assertEqual(report['training_rows'],2)
        cfg=load_config(overrides={'validation':{'test_size':.5,'auto_adjust_test_size':False}})
        self.assertEqual(training_split(4,cfg)[1]['validation_rows'],2)
    def test_both_engines_train_export_and_reload_four_to_seven_with_forty_features(self):
        evidence=[]
        with tempfile.TemporaryDirectory() as folder,threadpool_limits(limits=1):
            for records in range(4,8):
                for engine in ('optuna_v2','optuna_v3'):
                    with self.subTest(records=records,engine=engine):
                        data=mixed_fixture(records,40);output=Path(folder)/('%s_%d'%(engine,records))
                        overrides={'search':{'n_trials':2},'expert':{'scores':{'x0':5,'x4':4},'field_types':{'x4':'配置类型'}}}
                        if engine=='optuna_v3':overrides['v3']={'startup_trials':1,'warm_start':{'auto_history':False}}
                        _,report=train_price_optuna_v2(data,output,overrides=overrides,engine=engine,progress=lambda s:None)
                        self.assertEqual(report['training_rows'],records);self.assertEqual(report['feature_count'],40)
                        self.assertEqual(report['validation_train_rows'],records-2);self.assertEqual(report['validation_test_rows'],2)
                        self.assertEqual(report['min_samples'],4);self.assertTrue(report['tiny_sample_mode'])
                        self.assertTrue(report['selected_split_reload_verified']);self.assertLess(report['native_equivalence_max_delta_wan'],1e-5)
                        self.assertTrue(np.isfinite(report['validation_metrics']['r2']));self.assertIsNone(report['holdout_metrics']['r2'])
                        predictions=predict_batch(load_bundle(output/'price_native_bundle.pkl'),[r['params'] for r in data['agreements']])
                        self.assertEqual(len(predictions),records);self.assertTrue(all(r['predicted_price_wan']>0 for r in predictions))
                        split=json.loads((output/'validation_split.json').read_text(encoding='utf-8'))
                        self.assertEqual(split,report['validation_split']);self.assertEqual(split['requested_test_size'],.2)
                        evidence.append({'engine':engine,'records':records,'features':40,'training':records-2,'validation':2,'native_reload':True})
        self.assertEqual(len(evidence),8)

if __name__=='__main__':unittest.main(verbosity=2)
