# -*- coding: utf-8 -*-
import copy,json,sys,tempfile,time,unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import numpy as np,pandas as pd
from sklearn.dummy import DummyRegressor
from tools.product_price_tabpfn import load_config,_select,_fit_bank,factory
from tools.price_candidate_recovery import NoUsablePriceCandidates,positive_predictions
from tools.product_price_automl_v2 import FitWorker,BudgetExpired


def negative_factory(candidate,seed,specs,config):
    return DummyRegressor(strategy='constant',constant=-1.)


class Worker:
    def __init__(self,bad=None):self.bad=bad
    def fit(self,candidate,seed,specs,frame,y,validation,return_model=False):
        if self.bad and self.bad(candidate,seed):raise ValueError('候选产生非有限或非正价格')
        extra=2 if candidate['family']=='tabpfn' else 0
        return 10+validation.x0.to_numpy()+extra,None


class CandidateRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.holder=tempfile.TemporaryDirectory(prefix='candidate_recovery_',dir=str(ROOT/'outputs'))
        self.directory=Path(self.holder.name);assert ROOT/'outputs' in self.directory.resolve().parents
        self.X=pd.DataFrame({'x0':np.arange(8,dtype=float),'x1':np.arange(8,dtype=float)**2})
        self.y=10+self.X.x0.to_numpy();self.groups=np.arange(8)
        self.specs=[{'parameter_id':key,'label':'属性'+key,'value_type':'number'} for key in self.X]
        self.config=load_config(overrides={'validation':{'protocol':'group_loo'},
             'tabpfn':{'variants':[{'target_transform':'identity','output_type':'median'}]},
             'baselines':{'ridge_alphas':[1.],'select_k':['all'],'bayesian_ridge':False}})
    def tearDown(self):self.holder.cleanup()
    def select(self,worker,config=None):
        return _select(self.X,self.y,self.groups,self.specs,config or self.config,worker,self.directory/'cache',self.directory/'selection',lambda _:None)

    def test_real_worker_rejects_bad_prediction_but_accepts_positive_training_prices(self):
        cfg=copy.deepcopy(self.config);cfg['resources']['fit_timeout_seconds']=30
        worker=FitWorker(cfg,time.monotonic()+60,negative_factory)
        try:
            with self.assertRaises(ValueError) as caught:
                worker.fit({'model':'bad','family':'linear'},1,self.specs,self.X,self.y,self.X)
            self.assertIn('最小有限预测=-1.0',str(caught.exception))
            self.assertTrue(np.all(self.y>0))
        finally:worker.close()

    def test_failed_seed_expansion_keeps_complete_validated_prefix(self):
        bad_seed=int(np.random.SeedSequence(self.config['seed_bagging']['base']).generate_state(4)[1])
        selected=self.select(Worker(lambda candidate,seed:candidate['family']=='tabpfn' and seed==bad_seed))
        tab=selected['candidates'][selected['best_tabpfn']]
        self.assertEqual(len(tab['seeds']),1)
        self.assertEqual(tab['seed_stop_reason'],'expansion_failed_kept_validated_prefix')
        self.assertEqual(selected['failures'][-1]['action'],'retain_validated_seed_prefix')

    def test_nonmicro_later_seed_milestone_keeps_earlier_complete_bank(self):
        cfg=copy.deepcopy(self.config);cfg['validation']['protocol']='holdout_nested'
        bad_seed=int(np.random.SeedSequence(cfg['seed_bagging']['base']).generate_state(4)[3])
        selected=self.select(Worker(lambda candidate,seed:candidate['family']=='tabpfn' and seed==bad_seed),cfg)
        row=selected['candidates'][selected['best_tabpfn']]
        self.assertEqual(len(row['seeds']),2)
        self.assertEqual(row['seed_stop_reason'],'expansion_failed_kept_validated_prefix')
        self.assertEqual(selected['failures'][-1]['retained_seeds'],row['seeds'])

    def test_all_refitted_candidates_failing_stops_without_a_fake_ensemble(self):
        selected=self.select(Worker())
        with self.assertRaises(NoUsablePriceCandidates):
            _fit_bank(selected,self.X,self.y,self.X,self.specs,Worker(lambda candidate,seed:True),stage='final_refit')

    def test_all_failed_neural_candidates_do_not_discard_valid_classical_models(self):
        selected=self.select(Worker(lambda candidate,seed:candidate['family']=='tabpfn'))
        self.assertIsNone(selected['best_tabpfn'])
        self.assertAlmostEqual(sum(selected['weights']),1)
        self.assertTrue(np.all(selected['oof']>0))
        self.assertTrue(selected['failures'])

    def test_explicit_neural_requirement_is_not_silently_relaxed(self):
        cfg=copy.deepcopy(self.config);cfg['resilience']['require_tabpfn']=True
        with self.assertRaises(NoUsablePriceCandidates):
            self.select(Worker(lambda candidate,seed:candidate['family']=='tabpfn'),cfg)

    def test_zero_weight_neural_diagnostic_failure_does_not_abort_main_price_model(self):
        selected=self.select(Worker())
        self.assertLessEqual(selected['weights'][selected['best_tabpfn']],1e-8)
        bank,_=_fit_bank(selected,self.X,self.y,self.X,self.specs,Worker(lambda candidate,seed:candidate['family']=='tabpfn'),stage='outer_refit')
        np.testing.assert_allclose(bank.dot(selected['weights']),self.y,atol=1e-6)
        self.assertIsNone(selected['best_tabpfn'])
        self.assertEqual(selected['refit_recoveries'][0]['stage'],'outer_refit')

    def test_failed_weighted_member_promotes_validated_alternative(self):
        selected=self.select(Worker())
        bad=copy.deepcopy(selected['candidates'][int(np.argmax(selected['weights']))]['config'])
        bank,_=_fit_bank(selected,self.X,self.y,self.X,self.specs,Worker(lambda candidate,seed:candidate==bad),stage='final_refit')
        self.assertNotIn(bad,[row['config'] for row in selected['candidates']])
        np.testing.assert_allclose(bank.dot(selected['weights']),self.y,atol=1e-6)
        self.assertAlmostEqual(sum(selected['weights']),1)

    def test_recovery_weights_use_frozen_training_oof_targets(self):
        selected=self.select(Worker())
        original=selected['_selection_targets'].copy()
        bad=copy.deepcopy(selected['candidates'][int(np.argmax(selected['weights']))]['config'])
        _fit_bank(selected,self.X,self.y+1000,self.X,self.specs,Worker(lambda candidate,seed:candidate==bad),stage='final_refit')
        np.testing.assert_array_equal(selected['_selection_targets'],original)
        np.testing.assert_allclose(selected['oof'],self.y,atol=1e-6)

    def test_no_usable_candidate_stops_with_saved_diagnostics(self):
        with self.assertRaises(NoUsablePriceCandidates):self.select(Worker(lambda candidate,seed:True))
        report=json.loads((self.directory/'selection/candidate_report.json').read_text(encoding='utf-8'))
        self.assertEqual(report['status'],'no_usable_candidates')
        self.assertGreater(len(report['failures']),0)

    def test_global_budget_failure_is_not_mislabeled_as_a_bad_model(self):
        class TimedOut:
            def fit(self,*args,**kwargs):raise BudgetExpired('全局预算耗尽')
        with self.assertRaises(BudgetExpired):self.select(TimedOut())

    def test_invalid_output_is_not_repaired_by_epsilon_clipping(self):
        for value in (0.,-1.,float('nan'),float('inf')):
            with self.assertRaises(ValueError):positive_predictions([value],1)

    def test_log_and_median_baselines_are_actual_positive_models(self):
        y=.1+.01*self.X.x0.to_numpy()
        query=pd.DataFrame({'x0':[-15.],'x1':[225.]})
        for candidate in ({'model':'ridge','family':'linear','alpha':1e-6,'select_k':'all','target_transform':'log'},
                          {'model':'positive_median','family':'baseline'}):
            model=factory(candidate,1,self.specs,self.config).fit(self.X,y)
            result=model.predict(query)
            self.assertTrue(np.all(np.isfinite(result)) and np.all(result>0))


if __name__=='__main__':
    (ROOT/'outputs').mkdir(exist_ok=True)
    result=unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(CandidateRecoveryTests))
    (ROOT/'logs/candidate_recovery_test_report.json').write_text(json.dumps({'status':'PASS' if result.wasSuccessful() else 'FAIL',
        'tests':result.testsRun,'failures':len(result.failures),'errors':len(result.errors)},indent=2),encoding='utf-8')
    sys.exit(0 if result.wasSuccessful() else 1)