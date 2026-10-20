# -*- coding: utf-8 -*-
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
import pandas as pd
import optuna
from threadpoolctl import threadpool_limits
from product_optuna_v2_test import fixture
from product_optuna_v3_test import mixed_fixture
from tools.product_price_optuna_v2 import train_price_optuna_v2,load_config,dataset
from services.price_service.optuna_v2.cached_backend import CachedBackend
from services.price_service.optuna_v2.typed_features import TypedBackend
from services.price_service.optuna_v2.optimizer import _original_baseline
from services.price_service.optuna_v2.optimizer_v3 import SeedIndependentTPE


class V3FailureTests(unittest.TestCase):
    def test_real_finite_table_can_have_numerically_unscorable_candidate_without_aborting(self):
        _,_,_,data=fixture(4,2)
        for row,x,y in zip(data['agreements'],(0.,1e6,1e-6,2e6),(100.,200.,300.,400.)):
            row['params'].update(x0=x,x1=0.);row['historical_price_wan']=y
        with tempfile.TemporaryDirectory() as directory,threadpool_limits(limits=1):
            output=Path(directory)
            _,report=train_price_optuna_v2(data,output,engine='optuna_v3',overrides={'search':{'n_trials':10},'v3':{'startup_trials':1,'warm_start':{'auto_history':False}}},progress=lambda s:None)
            trials=pd.read_csv(output/'all_trials.csv');bad=trials[~trials['valid']]
            self.assertGreaterEqual(len(bad),1);self.assertTrue(all('FAIL' in s for s in bad.state))
            self.assertTrue(bad.r2.isna().all());self.assertTrue(bad.mape.isna().all())
            self.assertEqual(report['trials_completed'],10);self.assertEqual(report['candidate_pool_count'],10)
            self.assertEqual(report['failed_trials'],len(bad));self.assertLess(report['tpe_inherited_completed_trials'],6)
            diagnostic=json.loads((output/'v3_diagnostics.json').read_text(encoding='utf-8'))
            self.assertEqual(diagnostic['failed_trials'],len(bad));self.assertGreater(diagnostic['valid_trials'],0)
            self.assertTrue(any(f['diagnostic'].get('kind')=='nonfinite_metric' for f in diagnostic['failures']))
            history=json.loads((output/'study_history.json').read_text(encoding='utf-8'))
            self.assertTrue(all(t['user_attrs']['valid'] and np.isfinite(t['values']).all() for t in history['trials']))
            self.assertLess(report['native_equivalence_max_delta_wan'],1e-5)
    def test_tpe_keyerror_recovers_inside_original_distribution_and_records_each_parameter(self):
        sampler=SeedIndependentTPE(42,1,24);study=optuna.create_study(directions=['maximize','minimize'],sampler=sampler)
        with mock.patch.object(optuna.samplers.TPESampler,'sample_independent',side_effect=KeyError('Nonfinite metric')):
            trial=study.ask();alpha=trial.suggest_float('ridge_alpha',1e-5,100.,log=True);kernel=trial.suggest_categorical('svr_kernel',['linear','rbf'])
        self.assertTrue(1e-5<=alpha<=100.);self.assertIn(kernel,('linear','rbf'))
        self.assertEqual(len(sampler.fallbacks_by_trial[trial.number]),2);self.assertEqual(len(sampler.fallbacks),2)
        study.tell(trial,[.5,.1])
    def test_tpe_nonfinite_parameter_is_never_used_to_fit_model(self):
        sampler=SeedIndependentTPE(42,1,24);study=optuna.create_study(directions=['maximize','minimize'],sampler=sampler)
        with mock.patch.object(optuna.samplers.TPESampler,'sample_independent',return_value=float('nan')):
            trial=study.ask();alpha=trial.suggest_float('ridge_alpha',1e-5,100.,log=True)
        self.assertTrue(np.isfinite(alpha));self.assertEqual(len(sampler.fallbacks),1);study.tell(trial,[.5,.1])
    def test_cache_lookup_error_refits_same_candidate_and_disables_fast_path(self):
        data=mixed_fixture();frame,y,_,mappings,_,_=dataset(data,load_config(overrides={'expert':{'field_types':{'x4':'配置类型'}}}));rows=np.arange(3,19)
        params=_original_baseline(7527);backend=CachedBackend(tuple(mappings));backend.searching=True
        with threadpool_limits(limits=1),mock.patch.object(backend,'_cached_evaluate',side_effect=KeyError('cached_field')) as failed:
            actual=backend.evaluate(params,frame,y,rows,{},.2,3)[0]
            diagnostic=dict(backend.last_diagnostic)
            again=backend.evaluate(params,frame,y,rows,{},.2,3)[0]
            expected=TypedBackend(tuple(mappings)).evaluate(params,frame,y,rows,{},.2,3)[0]
        self.assertEqual(failed.call_count,1);self.assertEqual(backend.stats['cache_fallbacks'],1);self.assertTrue(backend.fast_path_disabled)
        self.assertEqual(diagnostic['kind'],'cache_key_error');self.assertAlmostEqual(actual['mape'],expected['mape'],places=12);self.assertEqual(actual,again)
    def test_zero_cache_setting_uses_original_backend_without_fast_path(self):
        _,_,_,data=fixture();frame,y,*_=dataset(data,load_config());backend=CachedBackend(cache_entries=0);backend.searching=True
        with threadpool_limits(limits=1),mock.patch.object(backend,'_cached_evaluate',side_effect=AssertionError('disabled path called')):
            score=backend.evaluate(_original_baseline(7527),frame,y,np.arange(3,19),{},.2,3)[0]
        self.assertTrue(np.isfinite(score['r2']));self.assertEqual(backend.stats['prediction_cache_hits'],0);self.assertEqual(backend.stats['model_fits'],6)
    def test_full_v3_training_finishes_even_if_tpe_sampler_raises_keyerror(self):
        data=mixed_fixture()
        with tempfile.TemporaryDirectory() as directory,threadpool_limits(limits=1),mock.patch.object(optuna.samplers.TPESampler,'sample_independent',side_effect=KeyError('TPE internal observation')):
            _,report=train_price_optuna_v2(data,Path(directory),engine='optuna_v3',overrides={'search':{'n_trials':5},'expert':{'field_types':{'x4':'配置类型'}},'v3':{'startup_trials':1,'warm_start':{'auto_history':False}}},progress=lambda s:None)
            self.assertGreater(report['sampler_fallback_count'],0);self.assertEqual(report['trials_completed'],5)
            self.assertTrue(report['selected_split_reload_verified']);self.assertTrue((Path(directory)/'price_native_bundle.pkl').exists())
            trials=pd.read_csv(Path(directory)/'all_trials.csv')
            self.assertEqual(sum(len(json.loads(value)) for value in trials['sampler_fallbacks']),report['sampler_fallback_count'])
    def test_all_unscorable_candidates_leave_diagnostics_without_fake_model(self):
        _,_,_,data=fixture()
        with tempfile.TemporaryDirectory() as directory,mock.patch.object(CachedBackend,'evaluate',side_effect=ValueError('Nonfinite metric')):
            output=Path(directory)
            with self.assertRaisesRegex(RuntimeError,'Every Optuna trial failed'):
                train_price_optuna_v2(data,output,engine='optuna_v3',overrides={'search':{'n_trials':4},'v3':{'startup_trials':1,'warm_start':{'auto_history':False}}},progress=lambda s:None)
            diagnostic=json.loads((output/'v3_diagnostics.json').read_text(encoding='utf-8'))
            self.assertEqual(diagnostic['valid_trials'],0);self.assertEqual(diagnostic['failed_trials'],4)
            self.assertFalse((output/'price_native_bundle.pkl').exists());self.assertTrue((output/'all_trials.csv').exists())

if __name__=='__main__':unittest.main(verbosity=2)
