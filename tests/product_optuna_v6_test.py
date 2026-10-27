# -*- coding: utf-8 -*-
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest import mock
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
from product_optuna_v3_test import mixed_fixture
from automl_pipeline.product_price_optuna_v6 import load_config,train_price_optuna_v6
from services.price_service.optuna_v6.protocol import make_plan,rank
from services.price_service.optuna_v6.blend import weights_on_training_oof
from services.price_service.native_bundle import predict,predict_batch


def data_fixture(n=16,p=36):
    data=mixed_fixture(n,p);rng=np.random.RandomState(731);z=np.linspace(-1.,1.,n)
    for i,row in enumerate(data['agreements']):
        for j in range(p):
            if j==2:value=int(np.clip(round((z[i]+1.)*2)+1,1,5))
            elif j==3:value='甲' if i%2 else '乙'
            elif j==4:value='001' if i%3 else '002'
            else:
                scale=[1.,80.,7200.][j%3];value=float((2.+z[i]+rng.normal(0,.003))*scale)
            row['params']['x%d'%j]=value
        row['historical_price_wan']=float(45.*np.exp(.35*z[i]+rng.normal(0,.003)))
    return data


def options(trials=5):
    return {'search':{'n_trials':trials,'timeout_seconds':None},'expert':{'field_types':{'x3':'enum','x4':'enum'},'scores':{'x0':5,'x1':4}},
            'v6':{'families':['ridge','lasso'],'budget':{'max_member_fits':800,'tiny_trials':trials},
                  'persistence':{'enabled':False},'selection':{'min_trials':3}}}


class V6Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name);cls.data=data_fixture();cls.logs=[]
        with mock.patch('services.price_service.optuna_v5.engine.run',side_effect=AssertionError('V3/V5 training must not run')):
            cls.bundle,cls.report=train_price_optuna_v6(cls.data,cls.root/'main',overrides=options(),progress=cls.logs.append)
    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()
    def test_independent_mainline_hits_both_targets_on_predictable_noisy_16x36(self):
        self.assertTrue(self.report['targets_met']);self.assertFalse(self.report['v3_used_for_selection'])
        self.assertLessEqual(self.report['train_metrics']['mape_percent'],10.)
        self.assertGreaterEqual(self.report['train_metrics']['r2'],.9)
        self.assertLessEqual(self.report['test_metrics']['mape_percent'],10.)
        self.assertGreaterEqual(self.report['test_metrics']['r2'],.9)
        self.assertGreater(self.report['test_metrics']['mape_percent'],1e-6)
    def test_feasible_tradeoff_outranks_infeasible_champion(self):
        cfg=load_config();old={'qualified':False,'train_violation':0.,'validation_violation':.2,'validation':{'mape_percent':5.,'r2':.7,'p90_ape_percent':8.}}
        new={'qualified':True,'train_violation':0.,'validation_violation':0.,'validation':{'mape_percent':6.,'r2':.95,'p90_ape_percent':9.}}
        self.assertLess(rank(new,cfg),rank(old,cfg))
    def test_every_outer_record_is_held_out_and_weights_frozen_before_reveal(self):
        seen=[]
        for result in self.report['outer_results']:
            self.assertFalse(set(result['train_rows'])&set(result['validation_rows']))
            self.assertFalse(result['test_labels_used_for_training']);seen.extend(result['validation_indices'])
        self.assertEqual(sorted(seen),list(range(16)))
    def test_native_reload_prediction_and_explanation(self):
        rows=[r['params'] for r in self.data['agreements']];batch=predict_batch(self.bundle,rows)
        for row,value in zip(rows,batch):self.assertEqual(value['predicted_price_wan'],predict(self.bundle,row)['predicted_price_wan'])
        from services.price_service.app import PriceService
        value=PriceService(self.root/'main/price_native_bundle.pkl').handle_post('/api/v1/predict',{'parameters':rows[0]})
        self.assertAlmostEqual(sum(c['ensemble_weight']*m['weight']*m['predicted_price_wan'] for c in value['explanation']['configuration_details'] for m in c['members']),value['prediction']['predicted_price_wan'],places=5)
    def test_repeated_oof_rows_do_not_unlock_tiny_weight_learning(self):
        cfg=load_config();y=np.arange(1.,10.)+10.;P=np.c_[y*.9,y*1.1]
        weights,source=weights_on_training_oof(P,y,cfg,sample_count=3)
        np.testing.assert_array_equal(weights,[.5,.5]);self.assertEqual(source,'preset_equal_small_sample')
    def test_four_through_seven_single_record_outer_and_inner_validation(self):
        for n in range(4,8):
            with self.subTest(n=n):
                _,r=train_price_optuna_v6(data_fixture(n,40),self.root/('tiny_'+str(n)),overrides=options(3),progress=lambda s:None)
                self.assertEqual(r['outer_rounds'],n);self.assertEqual(r['test_metrics']['rows'],n)
                self.assertIsNotNone(r['test_metrics']['r2'])
                for outer in r['outer_results']:
                    self.assertEqual(len(outer['validation_rows']),1);self.assertIsNone(outer['test_metrics']['r2'])
                self.assertTrue(all(len(f['train_rows'])>=2 for parts in r['evaluation_plan']['inner'] for f in parts))
    def test_first_outer_price_change_does_not_change_that_round_model_or_prediction(self):
        data=data_fixture(6,40);a,r=train_price_optuna_v6(data,self.root/'sealed_a',overrides=options(3),progress=lambda s:None)
        held=r['outer_results'][0]['validation_indices'];changed=copy.deepcopy(data)
        for i in held:changed['agreements'][i]['historical_price_wan']*=20.
        b,s=train_price_optuna_v6(changed,self.root/'sealed_b',overrides=options(3),progress=lambda s:None)
        for key in ('prediction','weights','selected_specs','train_rows'):
            self.assertEqual(r['outer_results'][0][key],s['outer_results'][0][key])
        self.assertNotEqual(r['outer_results'][0]['test_metrics'],s['outer_results'][0]['test_metrics'])
    def test_locked_holdout_exports_exact_assessed_model_and_excludes_held_labels(self):
        data=data_fixture(7,40);opts=options(3)
        opts['expert']['field_types']={p['parameter_id']:('enum' if p['parameter_id'] in ('x3','x4') else 'number') for p in data['parameters']}
        opts['v6']['evaluation']={'mode':'locked_holdout','holdout_ids':[data['agreements'][-1]['agreement_id']]}
        a,r=train_price_optuna_v6(data,self.root/'locked_a',overrides=opts,progress=lambda s:None)
        import hashlib
        self.assertEqual(hashlib.sha256((self.root/'locked_a/price_ensemble_bundle.pkl').read_bytes()).hexdigest(),r['outer_results'][0]['model_sha256_before_test'])
        changed=copy.deepcopy(data);changed['agreements'][-1]['historical_price_wan']*=25
        b,s=train_price_optuna_v6(changed,self.root/'locked_b',overrides=opts,progress=lambda s:None)
        self.assertEqual(r['weights'],s['weights']);self.assertEqual(r['member_ledger'],s['member_ledger'])
        self.assertEqual(predict(a,data['agreements'][-1]['params'])['predicted_price_wan'],predict(b,data['agreements'][-1]['params'])['predicted_price_wan'])
        self.assertEqual(a['residual_calibration'],b['residual_calibration'])
        self.assertEqual(predict(a,data['agreements'][-1]['params'])['price_interval_wan'],predict(b,data['agreements'][-1]['params'])['price_interval_wan'])
        self.assertFalse(r['interval_uses_test_labels']);self.assertIn('training',a['residual_calibration']['source'])
        self.assertFalse(set(r['fit_record_ids'])&set(r['holdout_record_ids']))
    def test_shuffled_prices_do_not_get_false_high_qualification(self):
        data=data_fixture();rng=np.random.RandomState(909);prices=[r['historical_price_wan'] for r in data['agreements']]
        for row,p in zip(data['agreements'],rng.permutation(prices)):row['historical_price_wan']=float(p)
        _,r=train_price_optuna_v6(data,self.root/'shuffled',overrides=options(5),progress=lambda s:None)
        self.assertFalse(r['targets_met']);self.assertLess(r['test_metrics']['r2'],.9)
    def test_resume_reuses_completed_outer_results_without_new_fits(self):
        opts=options(3);opts['v6']['persistence']={'enabled':True,'directory':str(self.root/'resume')}
        data=data_fixture(4,40)
        _,a=train_price_optuna_v6(data,self.root/'resume_a',overrides=opts,progress=lambda s:None)
        _,b=train_price_optuna_v6(data,self.root/'resume_b',overrides=opts,progress=lambda s:None)
        self.assertTrue(b['checkpoint_restored']);self.assertEqual(b['model_fits_this_session'],0)
        self.assertEqual(a['test_metrics'],b['test_metrics']);self.assertEqual(a['weights'],b['weights'])
    def test_interrupted_stage_resumes_sealed_process(self):
        from services.price_service.optuna_v6.backend import Backend
        opts=options(3);opts['v6']['persistence']={'enabled':True,'directory':str(self.root/'interrupted')}
        data=data_fixture(4,40);original=Backend.fit_bundle;calls=[]
        def interrupt(instance,*args,**kwargs):
            calls.append(1)
            if len(calls)==10:raise KeyboardInterrupt('controlled interruption')
            return original(instance,*args,**kwargs)
        with mock.patch.object(Backend,'fit_bundle',interrupt):
            with self.assertRaises(KeyboardInterrupt):train_price_optuna_v6(data,self.root/'interrupted_a',overrides=opts,progress=lambda s:None)
        _,r=train_price_optuna_v6(data,self.root/'interrupted_b',overrides=opts,progress=lambda s:None)
        self.assertTrue(r['outer_assessment_complete']);self.assertTrue(r['checkpoint_restored'])
    def test_fit_budget_counts_real_attempts_and_is_not_exceeded(self):
        opts=options(8);opts['v6']['budget']['max_member_fits']=200
        _,r=train_price_optuna_v6(data_fixture(),self.root/'budget',overrides=opts,progress=lambda s:None)
        self.assertLessEqual(r['fit_statistics']['total_attempts'],200)
        lines=(self.root/'budget/transient_search').glob('fit_ledger.jsonl')
        for path in lines:
            events=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            self.assertEqual(sum(e['event']=='fit_start' for e in events),r['fit_statistics']['total_attempts'])
    def test_partial_screen_scores_cannot_become_champions(self):
        opts=options(15);opts['v6']['selection']['continue_after_targets']=True
        opts['v6']['screening']={'startup_candidates':1,'keep_quantile':.1,'exploration_fraction':0.}
        _,r=train_price_optuna_v6(data_fixture(),self.root/'screen',overrides=opts,progress=lambda s:None)
        self.assertGreater(sum(x['pruned_candidates'] for x in r['outer_results'])+r['final_pruned_candidates'],0)
        for x in r['outer_results']:
            if x['inner_selection_metrics']:self.assertEqual(x['inner_selection_metrics']['validation']['rows'],len(x['train_rows']))
    def test_trace_contains_every_stage_and_real_precision(self):
        events=[json.loads(x) for x in (self.root/'main/training_events.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue({'v6_plan_sealed','v6_resource_plan','v6_outer_sealed','v6_features_fitted','v6_candidate_end','v6_outer_result','v6_evaluation_complete','v6_native_export_end','v6_report_saved','v6_pipeline_end'}<={e['event'] for e in events})
        self.assertTrue(all(e['best_inner_ensemble']['validation']['rows']>1 for e in events if e['event']=='v6_candidate_end' and 'best_inner_ensemble' in e))
    def test_strict_unmet_never_claims_ready_qualified(self):
        opts=options(3);opts['targets']={'mape_percent_max':1e-12,'r2_min':1.};opts['v6']['publish']={'policy':'require_targets'}
        with self.assertRaisesRegex(ValueError,'旧运行版本保留'):train_price_optuna_v6(data_fixture(4,40),self.root/'strict',overrides=opts,progress=lambda s:None)
        r=json.loads((self.root/'strict/training_report.json').read_text(encoding='utf-8'))
        self.assertFalse(r['targets_met']);self.assertTrue(r['quality_gate_rejected'])
    def test_1_to_5_prices_expert_scores_and_pure_data_are_separate(self):
        opts=options(3);opts['expert']['enabled']=False
        b,r=train_price_optuna_v6(data_fixture(4,40),self.root/'pure',overrides=opts,progress=lambda s:None)
        self.assertFalse(r['expert_scores_enabled']);self.assertEqual(next(s for s in b['automl_explanation']['price_attribute_scores'] if s['field_id']=='x0')['score'],5)
    def test_group_isolation_is_kept_when_inputs_repeat(self):
        from automl_pipeline.product_price_optuna_v2 import dataset
        data=data_fixture(6,40);data['agreements'][1]['params']=dict(data['agreements'][0]['params'])
        cfg=load_config(overrides=options(3));frame,y,specs,mappings,groups,ids=dataset(data,cfg)
        plan=make_plan(frame,np.arange(6),ids,cfg)
        for f in plan['outer']:self.assertFalse(set(plan['groups'][f['train']])&set(plan['groups'][f['validation']]))
    def test_all_original_families_are_available_without_v3_filter(self):
        opts=options(7);opts['v6']['families']=['ridge','lasso','svr','expert_tree','extratrees','gbdt'];opts['v6']['budget']['tiny_trials']=7
        _,r=train_price_optuna_v6(data_fixture(4,40),self.root/'all_families',overrides=opts,progress=lambda s:None)
        self.assertTrue(r['outer_assessment_complete']);self.assertFalse(r['v3_used_for_selection'])


if __name__=='__main__':unittest.main(verbosity=2)
