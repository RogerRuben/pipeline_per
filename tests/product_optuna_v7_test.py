# -*- coding: utf-8 -*-
import copy,hashlib,json,sys,tempfile,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
import pandas as pd
from product_optuna_v6_test import data_fixture
from automl_pipeline.product_price_optuna_v7 import load_config,baseline_config,train_price_optuna_v7,history_references
from automl_pipeline.product_price_optuna_v2 import train_price_optuna_v2
from services.price_service.optuna_v7.importance import response_importance,grade_consistency
from services.price_service.optuna_v7.review import make_folds,guard
from services.price_service.optuna_v7.backend import LogMeanRegressor
from services.price_service.optuna_v4.native_member import predict_saved
from services.price_service.native_bundle import predict,predict_batch,load_bundle

def options():
    return {'search':{'n_trials':3,'timeout_seconds':None},'expert':{'field_types':{'x3':'enum','x4':'enum'},'scores':{'x0':5,'x1':1}},
        'v7':{'review':{'max_candidates':2},'budget':{'extra_fit_ratio':1.,'minimum_extra_fits':240,'max_extra_fits':500,'extra_seconds':120},
              'model_seeds':{'count':2},'persistence':{'enabled':False}}}

class FixedLogModel(object):
    def __init__(self,price):self.price=price
    def predict(self,X):return np.full(len(X),np.log(self.price))

class V7Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.data=data_fixture(16,36)
        cls.bundle,cls.report=train_price_optuna_v7(cls.data,cls.root/'main',overrides=options(),progress=lambda s:None)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_original_core_byte_identity(self):
        self.assertEqual(hashlib.sha256((ROOT/'services/price_service/optuna_v2/price_automl_core.py').read_bytes()).hexdigest(),'f8f9877029e13535b97b40c429fb4afe6b5d613db635b6bee83307fca30b2899')
    def test_warm_start_file_content_changes_its_reuse_identity(self):
        cfg=load_config();path=self.root/'warm_params.json';path.write_text('{"version":1}',encoding='utf-8');cfg['v3']['warm_start']['config_file']=str(path)
        first=history_references(cfg);path.write_text('{"version":2}',encoding='utf-8');second=history_references(cfg)
        self.assertNotEqual(first['config_file']['sha256'],second['config_file']['sha256'])
    def test_exact_original_v3_baseline_prediction_and_parameters(self):
        cfg=self.report['baseline_v3']['search_config'];pure,r=train_price_optuna_v2(self.data,self.root/'pure',overrides=cfg,engine='optuna_v3',progress=lambda s:None)
        self.assertEqual(r['selected_parameters'],self.report['baseline_v3']['selected_parameters'])
        saved=load_bundle(self.root/'main/baseline_v3/price_native_bundle.pkl')
        for row in self.data['agreements']:
            self.assertEqual(predict(pure,row['params'])['predicted_price_wan'],predict(saved,row['params'])['predicted_price_wan'])
    def test_development_scopes_are_not_called_independent_test(self):
        self.assertFalse(self.report['independent_test_provided']);self.assertIsNone(self.report['test_metrics']['r2'])
        self.assertTrue(self.report['weights_fitted_on_validation']);self.assertIsNotNone(self.report['baseline_v3']['primary'])
    def test_export_reload_single_batch_and_weighted_sum(self):
        rows=[r['params'] for r in self.data['agreements']];batch=predict_batch(self.bundle,rows)
        self.assertLess(self.report['native_equivalence_max_delta_wan'],1e-5)
        for row,result in zip(rows,batch):self.assertEqual(predict(self.bundle,row)['predicted_price_wan'],result['predicted_price_wan'])
    def test_enhancement_fits_stay_under_actual_limit(self):
        stats=self.report['fit_statistics'];self.assertLessEqual(stats['extra_attempts_total'],stats['extra_fit_limit'])
    def test_seed_mean_and_member_removal_are_explicit_review_variants(self):
        reviews=self.report['candidate_reviews']
        self.assertTrue(any('__drop_' in r['recipe']['id'] for r in reviews))
        self.assertTrue(any(len(r['recipe'].get('model_seeds',[0]))>1 for r in reviews))
    def test_common_comparison_preserves_non_degradation_guard(self):
        self.assertEqual(self.report['evaluation_plan']['coverage_per_record'],[1]*16)
        self.assertFalse(self.report['evaluation_plan']['uses_prices_for_partitioning'])
        if self.report['enhancement_applied']:
            self.assertLessEqual(self.report['common_development_metrics']['mape_percent'],self.report['baseline_v3']['common_development']['mape_percent']+1e-10)
    def test_seed_average_is_in_price_space(self):
        wrapper=LogMeanRegressor([FixedLogModel(1),FixedLogModel(100)],[0,9])
        np.testing.assert_allclose(np.exp(wrapper.predict(np.zeros((2,1)))),[50.5,50.5])
    def test_grade_counts_do_not_supply_individual_answers(self):
        cfg=load_config()['v7']['consistency'];cfg['mode']='strict'
        imp={'normalization_available':True,'attributes':[{'field_id':'x%d'%i,'importance_percent':float(36-i)} for i in range(36)]}
        scores={'x%d'%i:(5 if i<8 else 4 if i<16 else 3 if i<24 else 2 if i<32 else 1) for i in range(36)}
        good=grade_consistency(imp,scores,list(scores),cfg);self.assertTrue(good['passed']);self.assertEqual(good['wrong_attributes'],0)
        changed=dict(scores);changed['x0'],changed['x35']=changed['x35'],changed['x0']
        bad=grade_consistency(imp,changed,list(scores),cfg);self.assertEqual(bad['wrong_attributes'],2)
        self.assertFalse(bad['mapping_uses_individual_expert_labels'])
        more=dict(changed);more['x1'],more['x34']=more['x34'],more['x1']
        self.assertFalse(grade_consistency(imp,more,list(scores),cfg)['passed'])
    def test_ties_and_unrated_attributes_are_not_forged_matches(self):
        cfg=load_config()['v7']['consistency'];cfg['mode']='strict'
        imp={'normalization_available':True,'attributes':[{'field_id':'a','importance_percent':50.},{'field_id':'b','importance_percent':50.},{'field_id':'c','importance_percent':0.}]}
        r=grade_consistency(imp,{'a':5,'b':1},['a','b','c'],cfg)
        self.assertFalse(r['passed']);self.assertEqual(r['ambiguous_attributes'],2);self.assertEqual(r['unrated_attributes'],['c'])
    def test_importance_invariant_to_numeric_unit_conversion(self):
        cfg=load_config()['v7']['consistency'];f=pd.DataFrame({'a':[1.,2.,3.,4.],'b':[2.,3.,1.,4.]})
        first=response_importance(lambda X:10+X.a.to_numpy()*2+X.b.to_numpy(),f,cfg)
        large=f.copy();large['a']*=7200
        second=response_importance(lambda X:10+X.a.to_numpy()/7200*2+X.b.to_numpy(),large,cfg)
        np.testing.assert_allclose([x['importance_percent'] for x in first['attributes']],[x['importance_percent'] for x in second['attributes']],atol=1e-10)
    def test_four_through_seven_samples_and_forty_attributes(self):
        for n in range(4,8):
            with self.subTest(n=n):
                b,r=train_price_optuna_v7(data_fixture(n,40),self.root/('tiny'+str(n)),overrides=options(),progress=lambda s:None)
                self.assertEqual(r['training_rows'],n);self.assertEqual(r['evaluation_plan']['coverage_per_record'],[1]*n)
                self.assertTrue(all(len(f['train_rows'])>=2 and len(f['validation_rows'])==1 for f in r['evaluation_plan']['folds']))
    def test_completed_resume_has_zero_new_baseline_and_extra_fits(self):
        cfg=options();cfg['v7']['persistence']={'enabled':True,'directory':str(self.root/'checkpoints')}
        a,r=train_price_optuna_v7(data_fixture(4,8),self.root/'resume_a',overrides=cfg,progress=lambda s:None)
        b,s=train_price_optuna_v7(data_fixture(4,8),self.root/'resume_b',overrides=cfg,progress=lambda s:None)
        self.assertEqual(s['baseline_new_fits'],0);self.assertEqual(s['extra_fits_this_session'],0)
        self.assertEqual(r['weights'],s['weights']);self.assertEqual(r['deployed_training_metrics'],s['deployed_training_metrics'])
    def test_report_only_consistency_does_not_change_model_selection(self):
        cfg=options();cfg['v7']['consistency']={'mode':'report'}
        b,r=train_price_optuna_v7(self.data,self.root/'report_only',overrides=cfg,progress=lambda s:None)
        self.assertEqual(r['selected_model'],self.report['selected_model']);self.assertIsNotNone(r['expert_grade_consistency'])
    def test_unidentifiable_strict_expert_constraint_does_not_pass(self):
        cfg=options();cfg['expert']['scores']={'x%d'%i:3 for i in range(8)};cfg['v7']['consistency']={'mode':'strict'};cfg['v7']['publish']={'policy':'require_targets'}
        out=self.root/'strict'
        with self.assertRaises(ValueError):train_price_optuna_v7(data_fixture(4,8),out,overrides=cfg,progress=lambda s:None)
        r=json.loads((out/'training_report.json').read_text(encoding='utf-8'));self.assertFalse(r['targets_met']);self.assertFalse(r['expert_grade_consistency']['passed'])
    def test_baseline_only_has_no_enhancement_fits(self):
        cfg=options();cfg['v7']['mode']='baseline_only'
        b,r=train_price_optuna_v7(data_fixture(4,8),self.root/'baseline_only',overrides=cfg,progress=lambda s:None)
        self.assertFalse(r['enhancement_applied']);self.assertEqual(r['extra_fits_this_session'],0)
    def test_actual_saved_model_can_pass_strict_grades_without_rewriting_importance(self):
        from automl_pipeline.product_price_optuna_v2 import dataset
        import pickle
        data=data_fixture(10,8);cfg=options();cfg['expert']['enabled']=False;cfg['v7']['mode']='baseline_only'
        cfg['targets']={'mape_percent_max':30.,'r2_min':.5}
        first,r=train_price_optuna_v7(data,self.root/'grade_source',overrides=cfg,progress=lambda s:None)
        frame=dataset(data,load_config(overrides=cfg))[0]
        with (self.root/'grade_source/price_ensemble_bundle.pkl').open('rb') as handle:pack=pickle.load(handle)
        settings=load_config(overrides=cfg)['v7']['consistency'];imp=response_importance(lambda X:predict_saved(pack,X),frame,settings)
        # A constructed contract test, not a claim that real expert annotations are known.
        ranked=sorted(imp['attributes'],key=lambda x:-x['importance_percent']);levels=[5,4,4,3,3,2,2,1]
        cfg['expert']['scores']={a['field_id']:g for a,g in zip(ranked,levels)};cfg['v7']['consistency']={'mode':'strict'}
        second,s=train_price_optuna_v7(data,self.root/'grade_strict',overrides=cfg,progress=lambda s:None)
        self.assertTrue(s['expert_grade_consistency']['passed']);self.assertEqual(s['expert_grade_consistency']['error_percent'],0.)
        for row in data['agreements']:self.assertEqual(predict(first,row['params'])['predicted_price_wan'],predict(second,row['params'])['predicted_price_wan'])
    def test_guard_rejects_large_anchor_regression_even_below_ten_percent(self):
        base={'anchor_validation':{'mape_percent':2.,'r2':.99},'validation':{'mape_percent':12.,'r2':.8}}
        new={'anchor_validation':{'mape_percent':8.,'r2':.99},'validation':{'mape_percent':10.,'r2':.9}}
        self.assertFalse(guard(new,base,load_config()))
    def test_expert_strength_change_reuses_models_that_do_not_use_expert_scores(self):
        from services.price_service.optuna_v7.backend import Backend,FitLedger
        from services.price_service.optuna_v5.trace import TrainingTrace
        from services.price_service.optuna_v2.optimizer import _original_baseline
        frame=pd.DataFrame({'a':[1.,2.,3.,4.],'b':[2.,4.,1.,3.]});target=np.array([10.,15.,18.,22.]);out=self.root/'dependency_cache'
        trace=TrainingTrace(out,lambda s:None);ledger=FitLedger(out,10,60,trace)
        backend=Backend(frame,target,np.arange(4),{'a':5.,'b':1.},(),{},out,'dependency-test',ledger,trace)
        params=_original_baseline(42);params['et_n_estimators']=10
        recipe={'params':params,'family_weights':[0.,0.,0.,1.,0.,0.],'model_seeds':[0],'expert_strength':1.}
        backend.fit(np.arange(4),recipe,'first');before=ledger.used;recipe['expert_strength']=0.
        backend.fit(np.arange(4),recipe,'second');self.assertEqual(ledger.used,before);self.assertGreater(ledger.cache_hits,0)
    def test_interrupted_enhancement_reuses_original_baseline(self):
        from unittest import mock
        from services.price_service.optuna_v7.backend import Backend
        cfg=options();cfg['v7']['persistence']={'enabled':True,'directory':str(self.root/'partial')}
        original=Backend.fit;calls=[]
        def stop(instance,*args,**kwargs):
            calls.append(1)
            if len(calls)==3:raise KeyboardInterrupt('controlled interruption')
            return original(instance,*args,**kwargs)
        data=data_fixture(4,8)
        with mock.patch.object(Backend,'fit',stop):
            with self.assertRaises(KeyboardInterrupt):train_price_optuna_v7(data,self.root/'partial_a',overrides=cfg,progress=lambda s:None)
        b,r=train_price_optuna_v7(data,self.root/'partial_b',overrides=cfg,progress=lambda s:None)
        self.assertEqual(r['baseline_new_fits'],0);self.assertTrue(r['checkpoint_restored'])
    def test_logs_include_parameters_fits_folds_choices_and_export(self):
        events=[json.loads(s) for s in (self.root/'main/training_events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue({'v3_trial_complete','v7_member_fit_start','v7_review_fold','v7_selection_complete','v7_actual_model_check','v7_native_export_end','v7_report_saved'}.issubset({x['event'] for x in events}))

if __name__=='__main__':unittest.main(verbosity=2)
