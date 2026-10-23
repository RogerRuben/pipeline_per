# -*- coding: utf-8 -*-
import copy
import hashlib
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
from threadpoolctl import threadpool_limits
from product_optuna_v2_test import fixture,workbook
from product_optuna_v3_test import mixed_fixture
from automl_pipeline.product_price_optuna_v5 import load_config,train_price_optuna_v5
from automl_pipeline.product_price_optuna_v3 import load_config as v3_config,search_settings,train_price_optuna_v3
from services.price_service.optuna_v5.fusion import solve,dominates
from services.price_service.optuna_v5.persistence import save_state,load_state,BaselineObserver
from services.price_service.optuna_v2.cached_backend import CachedBackend
from services.price_service.optuna_v2.optimizer_v3 import run_training
from services.price_service.native_bundle import predict,predict_batch,load_bundle
from services.price_service.optuna_v4.native_member import predict_saved


def options(n=4):return {'search':{'n_trials':n},'expert':{'field_types':{'x3':'enum','x4':'enum'}},'v5':{'persistence':{'enabled':False},'audit':{'enabled':False}}}


class V5Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holder=tempfile.TemporaryDirectory();cls.path=Path(cls.holder.name);cls.data=mixed_fixture(7,40);cls.logs=[]
        cls.opts=options(6);cls.opts['targets']={'mape_percent_max':100.,'r2_min':-1000.};cls.opts['v5']['train_gate']={'mode':'off'}
        cls.bundle,cls.report=train_price_optuna_v5(cls.data,cls.path/'main',overrides=cls.opts,progress=cls.logs.append)
    @classmethod
    def tearDownClass(cls):cls.holder.cleanup()
    def test_preserves_qualified_reference_without_new_fit(self):
        r=self.report;self.assertTrue(r['baseline_preserved']);self.assertTrue(r['baseline_non_regression']);self.assertEqual(r['new_member_fits'],0)
        self.assertLessEqual(r['development_metrics']['mape_percent'],r['reference_metrics']['validation']['mape_percent']+1e-8)
        self.assertGreaterEqual(r['development_metrics']['r2'],r['reference_metrics']['validation']['r2']-1e-8)
    def test_v3_logs_and_trial_csv_contain_training_metrics(self):
        self.assertTrue(any('训练MAPE' in s and '开发MAPE' in s and '目标优胜' in s for s in self.logs))
        table=pd.read_csv(self.path/'main/baseline/all_trials.csv')
        self.assertTrue({'train_r2','train_mape_percent','train_rows'}.issubset(table.columns));self.assertFalse(table[table.valid].train_mape_percent.isna().any())
    def test_snapshot_and_full_refit_are_separate(self):
        for name in ('selected_snapshot_native.pkl','full_refit_native.pkl','v3_reference_bundle.pkl','price_native_bundle.pkl'):self.assertTrue((self.path/'main'/name).is_file())
        self.assertEqual(self.report['deployment_scope'],'selected_split');self.assertEqual(len(self.report['fit_record_ids']),5)
        self.assertEqual(self.report['full_refit_training_metrics']['rows'],7)
    def test_native_reload_single_batch_and_two_level_explanation(self):
        rows=[r['params'] for r in self.data['agreements']];batch=predict_batch(self.bundle,rows)
        for row,item in zip(rows,batch):self.assertEqual(item['predicted_price_wan'],predict(self.bundle,row)['predicted_price_wan'])
        from services.price_service.app import PriceService
        out=PriceService(self.path/'main/price_native_bundle.pkl').handle_post('/api/v1/predict',{'parameters':rows[0]})
        self.assertIn('reference_metrics',out['explanation']);self.assertEqual(len(out['explanation']['price_attribute_scores']),40)
        total=sum(d['ensemble_weight']*m['weight']*m['predicted_price_wan'] for d in out['explanation']['configuration_details'] for m in d['members'])
        self.assertAlmostEqual(total,out['prediction']['predicted_price_wan'],places=5)
    def test_inactive_poor_model_can_have_zero_weight(self):
        y=np.array([10.,20.,30.,40.]);P=np.c_[y,y*3.];cfg=load_config(overrides={'v5':{'persistence':{'enabled':False}}})
        w,s,info=solve(P,P,y,y,[1.,0.],cfg)
        self.assertLess(w[1],1e-8);self.assertTrue(s['qualified']);self.assertLess(s['validation']['mape_percent'],1e-7)
    def test_solver_failure_retains_feasible_baseline(self):
        y=np.array([10.,20.,30.,40.]);P=np.c_[y,y*3.];cfg=load_config()
        with mock.patch('services.price_service.optuna_v5.fusion.linprog',side_effect=ValueError('numerical LP')),mock.patch('services.price_service.optuna_v5.fusion.minimize',side_effect=RuntimeError('numerical SLSQP')):
            w,s,info=solve(P,P,y,y,[1.,0.],cfg)
        np.testing.assert_allclose(w,[1.,0.]);self.assertTrue(s['qualified']);self.assertTrue(info['notes'])
    def test_training_constraints_are_checked_on_actual_predictions(self):
        ytr=np.array([10.,20.,30.]);yva=np.array([15.,25.]);T=np.c_[ytr,ytr*3];V=np.c_[yva*1.05,yva]
        w,s,_=solve(T,V,ytr,yva,[1.,0.],load_config())
        self.assertTrue(s['qualified']);self.assertLess(s['train']['mape_percent'],10.+1e-8);self.assertGreater(s['train']['r2'],.9-1e-8)
    def test_constant_train_price_is_not_given_fake_r2(self):
        w,s,_=solve(np.array([[10.],[10.]]),np.array([[15.],[25.]]),np.array([10.,10.]),np.array([15.,25.]),[1.],load_config())
        self.assertIsNone(s['train']['r2']);self.assertFalse(s['qualified'])
    def test_source_immutable_core_and_objective_remain_original(self):
        import ast
        root=ROOT/'services/price_service/optuna_v2';p=json.loads((root/'SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256((root/'price_automl_core.py').read_bytes()).hexdigest(),p['original_core_sha256'])
        # The observer is outside the original objective: the original field
        # remains exactly the original function name, with no train-gate branch.
        tree=ast.parse((root/'optimizer.py').read_text(encoding='utf-8'));node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='objective')
        self.assertNotIn('train_metrics',ast.dump(node))
    def test_log_cache_reuses_both_predictions_without_refit(self):
        from services.price_service.optuna_v2.optimizer import _original_baseline
        X,y,_,_=fixture(7,8);frame=pd.DataFrame(X,columns=['x%d'%i for i in range(8)]);backend=CachedBackend();backend.searching=True;cfg=_original_baseline(7527)
        with threadpool_limits(limits=1):
            backend.evaluate(cfg,frame,y,np.arange(7),{},.3,3);fits=backend.stats['model_fits'];train=backend.last_evaluation['train_prediction'].copy()
            backend.evaluate(cfg,frame,y,np.arange(7),{},.3,3)
        self.assertEqual(backend.stats['model_fits'],fits);np.testing.assert_array_equal(train,backend.last_evaluation['train_prediction'])
    def test_4_to_7_rows_and_40_mixed_scale_features(self):
        for n in range(4,8):
            with self.subTest(rows=n):
                _,r=train_price_optuna_v5(mixed_fixture(n,40),self.path/('tiny_%d'%n),overrides=options(2),progress=lambda s:None)
                self.assertEqual(r['training_rows'],n);self.assertEqual(r['feature_count'],40);self.assertEqual(r['validation_test_rows'],2);self.assertLess(r['native_equivalence_max_delta_wan'],1e-5)
                self.assertEqual(r['attribute_types']['x4'],'配置类型')
    def test_audit_does_not_rerank_goal_first(self):
        opts=copy.deepcopy(self.opts);opts['v5']['audit']={'enabled':True,'max_splits':2}
        _,r=train_price_optuna_v5(self.data,self.path/'audited',overrides=opts,progress=lambda s:None)
        self.assertEqual(r['cross_split_audit']['status'],'completed');self.assertFalse(r['audit_used_for_selection'])
        self.assertAlmostEqual(r['development_metrics']['mape_percent'],self.report['development_metrics']['mape_percent'],places=9)
        self.assertAlmostEqual(r['development_metrics']['r2'],self.report['development_metrics']['r2'],places=9)
    def test_balanced_and_robust_scopes_are_explicit(self):
        for mode in ('balanced','robust'):
            opts=options(3);opts['v5'].update(mode=mode,audit={'enabled':True,'max_splits':2})
            _,r=train_price_optuna_v5(self.data,self.path/mode,overrides=opts,progress=lambda s:None)
            self.assertTrue(r['audit_used_for_selection']);self.assertEqual(r['cross_split_audit']['status'],'completed')
            if mode=='robust':self.assertEqual(r['cv_metrics']['scope'],'searched_cross_partition_development')
    def test_conditional_enhancement_spends_one_fit_per_member(self):
        opts=options(2);opts['v5']['enhancement']={'max_new_fits':6,'continue_after_targets':True}
        _,r=train_price_optuna_v5(mixed_fixture(4,40),self.path/'members',overrides=opts,progress=lambda s:None)
        self.assertEqual(r['new_member_fits'],6)
        rows=[json.loads(s) for s in (self.path/'members/enhancement_trials.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows),6);self.assertEqual({x['family'] for x in rows},{'ridge','lasso','svr','expert_tree','extratrees','gbdt'})
    def test_total_budget_and_extra_budget_are_distinct(self):
        opts=options(2);opts['v5'].update(budget={'max_member_fits':50},enhancement={'max_new_fits':4,'continue_after_targets':True})
        _,r=train_price_optuna_v5(mixed_fixture(4,40),self.path/'budget',overrides=opts,progress=lambda s:None)
        self.assertEqual(r['new_member_fits'],4);self.assertLessEqual(r['fit_statistics']['total'],50)
        with self.assertRaises(ValueError):load_config(overrides={'v5':{'budget':{'max_member_fits':10}}})
    def test_resume_can_extend_fit_budget_without_retraining_baseline(self):
        d=mixed_fixture(4,40);opts=options(2);opts['v5'].update(persistence={'enabled':True,'directory':str(self.path/'resume_state')},enhancement={'max_new_fits':2,'continue_after_targets':True})
        _,a=train_price_optuna_v5(d,self.path/'resume_a',overrides=opts,progress=lambda s:None)
        opts['v5']['enhancement']['max_new_fits']=4
        _,b=train_price_optuna_v5(d,self.path/'resume_b',overrides=opts,progress=lambda s:None)
        self.assertTrue(b['checkpoint_restored']);self.assertEqual(b['new_member_fits'],4)
        self.assertLess(b['model_fits_this_session'],b['fit_statistics']['total'])
        opts['v5']['enhancement']['max_new_fits']=4
        _,c=train_price_optuna_v5(d,self.path/'resume_c',overrides=opts,progress=lambda s:None)
        self.assertEqual(c['model_fits_this_session'],0)
        self.assertEqual(b['development_metrics'],c['development_metrics'])
    def test_changed_targets_or_data_are_not_reused_as_same_run(self):
        d=mixed_fixture(4,40);opts=options(2);opts['v5']['persistence']={'enabled':True,'directory':str(self.path/'identities')}
        _,a=train_price_optuna_v5(d,self.path/'id_a',overrides=opts,progress=lambda s:None)
        opts['targets']={'mape_percent_max':7.}
        _,b=train_price_optuna_v5(d,self.path/'id_b',overrides=opts,progress=lambda s:None)
        self.assertFalse(b['checkpoint_restored']);self.assertNotEqual(a['checkpoint_directory'],b['checkpoint_directory'])
    def test_checkpoint_digest_and_identity_are_validated(self):
        directory=self.path/'integrity';save_state(directory/'state',{'x':1},'a')
        self.assertEqual(load_state(directory/'state','a'),{'x':1})
        with self.assertRaises(ValueError):load_state(directory/'state','b')
        meta=json.loads((directory/'state.json').read_text(encoding='utf-8'));(directory/meta['blob']).write_bytes(b'corrupt')
        with self.assertRaisesRegex(ValueError,'摘要'):load_state(directory/'state','a')
    def test_outer_labels_do_not_enter_selection_or_refit(self):
        d=mixed_fixture(6,40);opts=options(3);opts['expert']['field_types']={p['parameter_id']:('enum' if p['parameter_id'] in ('x3','x4') else 'number') for p in d['parameters']}
        opts['v5']['evaluation']={'mode':'locked_holdout','holdout_ids':['O004','O005']}
        b,a=train_price_optuna_v5(d,self.path/'outer_a',overrides=opts,progress=lambda s:None)
        changed=copy.deepcopy(d);changed['agreements'][4]['historical_price_wan']*=10;changed['agreements'][5]['historical_price_wan']*=30
        z,c=train_price_optuna_v5(changed,self.path/'outer_b',overrides=opts,progress=lambda s:None)
        self.assertEqual(a['baseline_selected_parameters'],c['baseline_selected_parameters']);self.assertEqual(a['development_metrics'],c['development_metrics'])
        self.assertEqual(predict(b,d['agreements'][4]['params'])['predicted_price_wan'],predict(z,d['agreements'][4]['params'])['predicted_price_wan'])
        self.assertFalse(set(a['fit_record_ids'])&set(a['holdout_record_ids']));self.assertNotEqual(a['holdout_metrics']['mape_percent'],c['holdout_metrics']['mape_percent'])
    def test_strict_unmet_training_does_not_publish_as_qualified(self):
        opts=options(2);opts['targets']={'mape_percent_max':1e-12,'r2_min':1.};opts['v5']['publish']={'policy':'require_targets'}
        with self.assertRaisesRegex(ValueError,'旧运行版本保留'):
            train_price_optuna_v5(mixed_fixture(4,40),self.path/'strict',overrides=opts,progress=lambda s:None)
        r=json.loads((self.path/'strict/training_report.json').read_text(encoding='utf-8'));self.assertFalse(r['targets_met']);self.assertTrue(r['quality_gate_rejected'])
    def test_expert_scores_are_still_price_1_to_5_and_can_be_off(self):
        opts=options(2);opts['expert'].update(enabled=False,scores={'x0':5,'x4':4})
        b,r=train_price_optuna_v5(self.data,self.path/'no_experts',overrides=opts,progress=lambda s:None)
        scores=b['automl_explanation']['price_attribute_scores'];self.assertEqual(next(s['score'] for s in scores if s['field_id']=='x0'),5)
        self.assertFalse(r['expert_scores_enabled']);self.assertTrue(all(not s['enabled'] for s in scores))
    def test_invalid_config_is_rejected(self):
        for cfg in ({'v5':{'mode':'unknown'}},{'v5':{'enhancement':{'max_new_fits':-1}}},{'v5':{'enhancement':{'families':['typo']}}},{'input':{'min_samples':3}}):
            with self.assertRaises(ValueError):load_config(overrides=cfg)
    def test_verified_saved_v3_can_be_reused_without_fit(self):
        data=fixture(7,8)[-1];source=self.path/'reference_input.xlsx';source.write_bytes(workbook(data));base=self.path/'old_v3';(base/'price').mkdir(parents=True)
        train_price_optuna_v3(data,base/'price',input_path=source,overrides={'search':{'n_trials':3},'v3':{'warm_start':{'auto_history':False}}},progress=lambda s:None)
        (base/'prototype_manifest.json').write_text(json.dumps({'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest()}),encoding='utf-8')
        opts={'search':{'n_trials':3},'v5':{'baseline':{'reference_dir':str(base)},'persistence':{'enabled':False},'audit':{'enabled':False}}}
        with mock.patch('services.price_service.optuna_v2.price_automl_core.fit_ensemble',side_effect=AssertionError('should reuse fitted baseline')):
            _,r=train_price_optuna_v5(data,self.path/'referenced',input_path=source,overrides=opts,progress=lambda s:None)
        self.assertEqual(r['baseline_source'],'verified_saved_v3');self.assertEqual(r['model_fits_this_session'],0)
        opts['expert']={'scores':{'x2':5}}
        _,r=train_price_optuna_v5(data,self.path/'changed_reference',input_path=source,overrides=opts,progress=lambda s:None)
        self.assertEqual(r['baseline_source'],'v3_completed');self.assertGreater(r['model_fits_this_session'],0)


class V5BaselineResumeTests(unittest.TestCase):
    def test_interrupted_v3_sampler_resumes_same_parameters_and_scores(self):
        X,y,specs,_=fixture(7,8);frame=pd.DataFrame(X,columns=[p['parameter_id'] for p in specs]);cfg=v3_config(overrides={'search':{'n_trials':12},'v3':{'warm_start':{'auto_history':False}}})
        def execute(out,observer=None):
            backend=CachedBackend();backend.retain_estimators=True
            with threadpool_limits(limits=1):
                return run_training(v3_settings=search_settings(cfg),history_identity='resume-unit',model_backend=backend,observer=observer,
                    data_path='fixture',output_dir=str(out),n_trials=12,target='price_wan',test_size=.3,price_bins=3,r2_target=.9,mape_target=.1,
                    seed_low=1,seed_high=10000,optimizer_seed=42,drop_first=False,remove_outliers=False,verbose=False,
                    prepared_data=(frame,y,np.arange(7)+3),expert_scores={p:1. for p in frame.columns},progress_callback=lambda s:None,min_samples=4)
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);whole=execute(root/'whole');reference=pd.read_csv(root/'whole/all_trials.csv')
            for interrupt_at in (3,9):
                class Interrupted(BaselineObserver):
                    def after_trial(self,driver,study,trial,phase):
                        super().after_trial(driver,study,trial,phase)
                        if sum(driver.counts.values())==interrupt_at:
                            self.flush(driver,study,phase);raise RuntimeError('simulated process interruption')
                ck=root/('checkpoint_%d'%interrupt_at)
                with self.assertRaisesRegex(RuntimeError,'simulated'):
                    execute(root/('first_%d'%interrupt_at),Interrupted(ck,'unit',interval=2,progress=lambda s:None))
                resumed=execute(root/('resumed_%d'%interrupt_at),BaselineObserver(ck,'unit',interval=2,progress=lambda s:None))
                self.assertEqual(whole['selected_parameters'],resumed['selected_parameters']);self.assertEqual(whole['validation_metrics'],resumed['validation_metrics'])
                actual=pd.read_csv(root/('resumed_%d/all_trials.csv'%interrupt_at))
                pd.testing.assert_frame_equal(reference,actual)


if __name__=='__main__':unittest.main(verbosity=2)
