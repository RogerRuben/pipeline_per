# -*- coding: utf-8 -*-
import copy
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
import pandas as pd
from product_optuna_v3_test import mixed_fixture
from automl_pipeline.product_price_optuna_v5 import load_config,train_price_optuna_v5
from automl_pipeline.product_modes import descriptor,mode_key
from services.price_service.optuna_v5.fixed_cv import make_folds,combine,accept
from services.price_service.optuna_v5.fusion import solve
from services.price_service.optuna_v5.trace import TrainingTrace,score_text
from services.price_service.native_bundle import predict,predict_batch


def options(fits=8):
    return {'search':{'n_trials':3},'expert':{'field_types':{'x3':'enum','x4':'enum'}},
            'v5':{'protocol':'fixed_cv','persistence':{'enabled':False},
                  'cv':{'n_splits':2},'enhancement':{'max_new_fits':fits,'continue_after_targets':True}}}


class FixedCVTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.root=Path(cls.temp.name)
        cls.data=mixed_fixture(7,40);cls.logs=[]
        cls.bundle,cls.report=train_price_optuna_v5(cls.data,cls.root/'main',overrides=options(),progress=cls.logs.append)

    @classmethod
    def tearDownClass(cls):cls.temp.cleanup()

    def test_default_is_fixed_cv_and_full_data(self):
        cfg=load_config();self.assertEqual(cfg['v5']['protocol'],'fixed_cv')
        self.assertEqual(cfg['v5']['weight_policy'],'baseline_fixed');self.assertEqual(cfg['deployment']['model_scope'],'full_data')

    def test_old_solver_can_interpolate_development_without_base_model_training_on_it(self):
        # Both base models have 10% error; fitting weights on those same labels
        # yields zero error, without either model having observed their labels.
        y=np.array([10.,20.,30.,40.]);P=np.c_[y*.9,y*1.1]
        cfg=load_config();w,s,_=solve(P,P,y,y,[.7,.3],cfg)
        self.assertLess(s['validation']['mape_percent'],1e-7)
        self.assertAlmostEqual(w[0],.5,places=6)
        self.assertGreater(np.mean(np.abs(P.dot([.7,.3])-y)/y)*100,3.9)

    def test_all_rows_held_out_once_and_group_isolation(self):
        frame=pd.DataFrame({'x':[0,0,1,2,3,4,5]});rows=np.arange(7)+3
        folds,plan=make_folds(frame,rows,load_config()['v5']['cv'])
        self.assertEqual(plan['coverage_per_row'],[1]*7)
        for f in folds:
            self.assertFalse(set(f['train'])&set(f['validation']))
            self.assertFalse(set(frame.iloc[f['train']].x)&set(frame.iloc[f['validation']].x))

    def test_folds_ignore_price_values(self):
        frame=pd.DataFrame({'x':range(16)});cfg=load_config()['v5']['cv']
        a,pa=make_folds(frame,np.arange(16)+3,cfg);b,pb=make_folds(frame,np.arange(16)+3,cfg)
        self.assertEqual(pa,pb);self.assertFalse(pa['uses_prices_to_choose_folds'])

    def test_unpartitionable_identical_groups_fail_instead_of_leaking(self):
        with self.assertRaisesRegex(ValueError,'独立成品'):
            make_folds(pd.DataFrame({'x':[1,1,1,2]}),np.arange(4),load_config()['v5']['cv'])

    def test_weights_remain_bitwise_unchanged_and_records_are_complete(self):
        records=[json.loads(s) for s in (self.root/'main/enhancement_trials.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertTrue(records)
        self.assertEqual(sum(r['new_fits'] for r in records),self.report['new_member_fits'])
        for r in records:
            self.assertEqual(r['weights_before'],r['weights_after'])
            self.assertEqual(r['fold_plan_sha256'],self.report['fold_plan_sha256'])
            if r['valid']:
                self.assertIn('single_model',r);self.assertIn('candidate_ensemble',r)
                self.assertIn('best_ensemble',r);self.assertIn('incumbent_before',r)
        self.assertFalse(self.report['weights_fitted_on_validation'])
        self.assertTrue(self.report['audit_used_for_selection'])

    def test_ooof_predictions_match_report_and_cover_every_record(self):
        import csv
        from services.price_service.optuna_v4.metrics import metrics
        with (self.root/'main/fixed_cv_predictions.csv').open(encoding='utf-8-sig') as handle:rows=list(csv.DictReader(handle))
        self.assertEqual(len(rows),7);self.assertEqual(len({r['record_id'] for r in rows}),7)
        result=metrics([float(r['actual_price']) for r in rows],[float(r['predicted_price']) for r in rows])
        self.assertEqual(result,self.report['development_metrics'])

    def test_events_include_fits_folds_candidates_exports_and_pipeline_completion(self):
        events=[json.loads(s) for s in (self.root/'main/training_events.jsonl').read_text(encoding='utf-8').splitlines()]
        names={e['event'] for e in events}
        self.assertTrue({'pipeline_start','pipeline_end','v3_member_fit_start','v3_trial_complete',
                         'cv_member_fit_start','cv_fold_scores','candidate_start','candidate_end',
                         'native_export_end','training_report_saved','search_stop','weights_frozen'}.issubset(names))
        self.assertEqual([e['sequence'] for e in events],list(range(1,len(events)+1)))

    def test_numeric_precision_does_not_round_small_error_to_zero(self):
        text=score_text({'mape_percent':1e-8,'r2':.99999999,'rows':4})
        self.assertIn('1e-08',text);self.assertNotIn('0.000%',text)

    def test_heartbeat_appears_during_slow_fit(self):
        logs=[];trace=TrainingTrace(self.root/'heartbeat',logs.append,heartbeat_seconds=.01)
        with trace.span('slow_test',family='ridge'):time.sleep(.04)
        self.assertTrue(any('heartbeat' in s for s in logs))

    def test_deteriorating_cv_is_rejected_despite_better_selected_split(self):
        old=copy.deepcopy(self.report['reference_cv_metrics']);new=copy.deepcopy(old)
        new['validation']['mape_percent']+=1.;new['selected_split_mape']=0.
        ok,why=accept(new,old,old,load_config());self.assertFalse(ok);self.assertEqual(why,'pooled_cv_regression')

    def test_native_predictions_and_weight_ledger(self):
        rows=[r['params'] for r in self.data['agreements']]
        results=predict_batch(self.bundle,rows)
        for row,result in zip(rows,results):self.assertEqual(result['predicted_price_wan'],predict(self.bundle,row)['predicted_price_wan'])
        self.assertEqual(self.bundle['automl_explanation']['evaluation_scope'],'fixed_cross_validation_development')
        self.assertEqual(self.report['fit_record_ids'],self.report['development_record_ids'])

    def test_four_to_seven_by_forty_still_work(self):
        for n in range(4,8):
            with self.subTest(n=n):
                _,r=train_price_optuna_v5(mixed_fixture(n,40),self.root/('tiny_%d'%n),overrides=options(0),progress=lambda s:None)
                self.assertEqual(r['development_metrics']['rows'],n)
                self.assertEqual(r['cross_split_audit']['plan']['coverage_per_row'],[1]*n)

    def test_resume_extends_candidate_budget_without_repeating_completed_folds(self):
        opts=options(2);opts['v5']['persistence']={'enabled':True,'directory':str(self.root/'state')}
        _,a=train_price_optuna_v5(self.data,self.root/'first',overrides=opts,progress=lambda s:None)
        opts['v5']['enhancement']['max_new_fits']=4
        _,b=train_price_optuna_v5(self.data,self.root/'second',overrides=opts,progress=lambda s:None)
        self.assertTrue(b['checkpoint_restored']);self.assertEqual(b['new_member_fits'],4)
        _,c=train_price_optuna_v5(self.data,self.root/'third',overrides=opts,progress=lambda s:None)
        self.assertEqual(c['model_fits_this_session'],0);self.assertEqual(b['development_metrics'],c['development_metrics'])

    def test_outer_prices_never_enter_fixed_cv_or_final_refit(self):
        data=mixed_fixture(6,40);opts=options(2)
        opts['expert']['field_types']={p['parameter_id']:('enum' if p['parameter_id'] in ('x3','x4') else 'number') for p in data['parameters']}
        opts['v5']['evaluation']={'mode':'locked_holdout','holdout_ids':['O004','O005']}
        b,a=train_price_optuna_v5(data,self.root/'outer_a',overrides=opts,progress=lambda s:None)
        other=copy.deepcopy(data);other['agreements'][4]['historical_price_wan']*=10
        z,c=train_price_optuna_v5(other,self.root/'outer_b',overrides=opts,progress=lambda s:None)
        self.assertEqual(a['development_metrics'],c['development_metrics']);self.assertEqual(a['fold_plan_sha256'],c['fold_plan_sha256'])
        self.assertEqual(predict(b,data['agreements'][4]['params']),predict(z,data['agreements'][4]['params']))
        self.assertFalse(set(a['fit_record_ids'])&set(a['holdout_record_ids']))

    def test_legacy_and_fixed_cv_saved_modes_are_different(self):
        a=load_config();b=load_config(overrides={'v5':{'protocol':'legacy_split'}})
        b['deployment']['model_scope']=a['deployment']['model_scope']
        self.assertNotEqual(mode_key(descriptor('optuna_v5',a)),mode_key(descriptor('optuna_v5',b)))

    def test_legitimate_exact_cv_is_not_replaced_with_invented_error(self):
        y=np.array([10.,20.,30.,40.]);folds=[{'id':0,'train':np.array([2,3]),'validation':np.array([0,1]),'train_prediction':y[2:],'validation_prediction':y[:2]},
                                            {'id':1,'train':np.array([0,1]),'validation':np.array([2,3]),'train_prediction':y[:2],'validation_prediction':y[2:]}]
        s=combine([{'folds':folds}],[1.],y,load_config())
        self.assertEqual(s['validation']['mape_percent'],0.);self.assertEqual(s['validation']['r2'],1.)

    def test_development_weight_optimizers_are_never_called_in_fixed_protocol(self):
        with mock.patch('services.price_service.optuna_v5.fusion.linprog') as lp,mock.patch('services.price_service.optuna_v5.fusion.minimize') as sqp:
            train_price_optuna_v5(self.data,self.root/'no_meta_fit',overrides=options(2),progress=lambda s:None)
        lp.assert_not_called();sqp.assert_not_called()

    def test_candidate_pool_replaces_families_instead_of_adding_weight_degrees_of_freedom(self):
        ledger=self.report['member_ledger'];families=[m['parameters'] for m in ledger]
        actual=[]
        from services.price_service.optuna_v2.price_automl_core import MODEL_NAMES
        for params in families:actual.extend(n for n in MODEL_NAMES if params['weight_'+n]>0)
        self.assertEqual(len(actual),len(set(actual)))
        self.assertLessEqual(len(actual),6)

    def test_strict_cv_failure_is_saved_with_exception_and_never_claimed_qualified(self):
        opts=options(0);opts['targets']={'mape_percent_max':1e-12,'r2_min':1.}
        opts['v5']['publish']={'policy':'require_targets'}
        with self.assertRaisesRegex(ValueError,'旧运行版本保留'):
            train_price_optuna_v5(self.data,self.root/'strict',overrides=opts,progress=lambda s:None)
        r=json.loads((self.root/'strict/training_report.json').read_text(encoding='utf-8'))
        self.assertFalse(r['targets_met']);self.assertTrue(r['quality_gate_rejected'])
        events=(self.root/'strict/training_events.jsonl').read_text(encoding='utf-8')
        self.assertIn('pipeline_exception',events)

    def test_balanced_and_robust_use_fixed_cv_during_search(self):
        for mode in ('balanced','robust'):
            opts=options(2);opts['v5']['mode']=mode
            _,r=train_price_optuna_v5(self.data,self.root/('mode_'+mode),overrides=opts,progress=lambda s:None)
            self.assertEqual(r['cv_metrics']['scope'],'fixed_cross_validation_development')
            self.assertFalse(r['weights_fitted_on_validation']);self.assertTrue(r['audit_used_for_selection'])

    def test_uniform_fixed_with_experts_off_and_complete_fold_budget(self):
        opts=options(1);opts['expert'].update(enabled=False,scores={'x0':5})
        opts['v5']['weight_policy']='uniform_fixed'
        b,r=train_price_optuna_v5(self.data,self.root/'uniform',overrides=opts,progress=lambda s:None)
        self.assertFalse(r['expert_scores_enabled']);self.assertEqual(r['new_member_fits'],0)
        self.assertEqual(r['stop_reason'],'insufficient_budget_for_complete_candidate')
        self.assertTrue(all(abs(w-1/6)<1e-12 for w in r['weights'].values()))
        self.assertEqual(next(a for a in b['automl_explanation']['price_attribute_scores'] if a['field_id']=='x0')['score'],5)

    def test_windows_transient_replace_retries_without_deleting_previous_commit(self):
        from services.price_service.optuna_v5.persistence import atomic_json
        import os
        path=self.root/'atomic_retry.json';path.write_text('{"old":true}',encoding='utf-8')
        original=os.replace;calls=[]
        def occasionally_locked(source,destination):
            calls.append(1)
            if len(calls)<3:
                self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'old':True})
                raise PermissionError('simulated Windows transient sharing violation')
            return original(source,destination)
        with mock.patch('services.price_service.optuna_v5.persistence.os.replace',side_effect=occasionally_locked):atomic_json(path,{'new':True})
        self.assertEqual(len(calls),3);self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'new':True})

    def test_new_cv_protocol_configuration_counts_reused_v3_fits_as_historical(self):
        opts=options(0);opts['v5']['persistence']={'enabled':True,'directory':str(self.root/'shared_reference')}
        train_price_optuna_v5(self.data,self.root/'shared_first',overrides=opts,progress=lambda s:None)
        opts['v5']['weight_policy']='uniform_fixed'
        _,r=train_price_optuna_v5(self.data,self.root/'shared_second',overrides=opts,progress=lambda s:None)
        stats=r['fit_statistics']
        self.assertEqual(r['model_fits_this_session'],stats['enhancement']+stats['audit']+stats['selected_snapshot']+stats['full_refit'])

    def test_soft_gate_uses_configured_penalty(self):
        cfg=load_config();cfg['v5']['train_gate'].update(mode='soft',soft_penalty=100.)
        old=copy.deepcopy(self.report['reference_cv_metrics']);new=copy.deepcopy(old)
        new['validation']['mape_percent']-=.01;new['train_violation']+=.1
        ok,why=accept(new,old,old,cfg)
        self.assertFalse(ok);self.assertEqual(why,'soft_training_penalty_not_improved')
        cfg['v5']['train_gate']['soft_penalty']=0.
        self.assertTrue(accept(new,old,old,cfg)[0])


if __name__=='__main__':unittest.main(verbosity=2)
