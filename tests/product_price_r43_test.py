# -*- coding: utf-8 -*-
import copy
import json
import pickle
import sys
import tempfile
import time
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
# The embedded runtime's sitecustomize preloads its packaged console helper.
# Route subsequent app submodules to the source being tested, not that old copy.
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
from threadpoolctl import threadpool_limits
from tools.product_price_automl_r4 import load_config,InnerSearch,train_price_r4,score
from tools.product_r43_search import repeat_summary,risk_loss,robust_blend
from tools.product_r43_profiles import profile
from services.price_service.r4_estimators import PricePreparation,R4PriceRegressor
from services.price_service.r43_support import PriceTrustRegion
from services.price_service.native_bundle import load_bundle,predict,predict_batch,price_risk
from app.recommender import effective_price,agreement_matches,rank_agreements


def fixture(noise=False):
    rng=np.random.RandomState(608);X=rng.uniform(-1,1,(16,36));X[:,0]=np.linspace(-1,1,16)
    y=80+30*X[:,0]**2+rng.normal(0,1.,16)
    if noise:y=np.random.RandomState(809).uniform(50,150,16)
    specs=[{'parameter_id':'x%d'%i,'label':'属性%d'%i,'value_type':'number','unit':'','required':False,'inference':{'observed_count':16}} for i in range(36)]
    data={'products':[{'product_code':'R43_TEST','product_name':'微样本验证'}],'parameters':specs,
          'agreements':[{'agreement_id':'T%d'%i,'product_code':'R43_TEST','historical_price_wan':float(y[i]),'params':{p['parameter_id']:float(X[i,j]) for j,p in enumerate(specs)}} for i in range(16)]}
    return X,y,specs,data


def small_config():
    c=profile('quick');c['models']=['ridge','poly_ridge','bayesian_ridge','ard']
    c['search'].update(anchor_trials=8,warm_start=False,max_restarts=0)
    c['pipeline'].update(feature_views=['screened'],select_k=[1,2,3],target_transforms=['identity'],stability_resamples=8)
    c['seed_search'].update(max_trials=4,minimum_trials=4,milestones=[4])
    c['validation'].update(inner_strategy='group_kfold',inner_folds=4,inner_repeats=2,outer_strategy='group_kfold',outer_folds=4,outer_repeats=2)
    c['resources'].update(total_seconds=120.,per_search_seconds=12.)
    return load_config(overrides=c)


class R43Tests(unittest.TestCase):
    def setUp(self):
        self.holder=tempfile.TemporaryDirectory();self.path=Path(self.holder.name)
        self.X,self.y,self.specs,self.data=fixture()
    def tearDown(self):self.holder.cleanup()
    def test_repeat_errors_cannot_cancel_in_primary_metrics(self):
        predictions=np.r_[self.y*.8,self.y*1.2]
        r=repeat_summary(self.y,predictions,2)
        self.assertAlmostEqual(r['pooled']['mape_percent'],20.)
        self.assertLess(r['averaged_prediction_metrics']['mape_percent'],1e-10)
        self.assertGreater(risk_loss(np.tile(self.y,2),predictions,.7,2,.15),risk_loss(np.tile(self.y,2),np.tile(self.y*1.1,2),.7,2,.15))
    def test_fold_predictions_are_retained_per_independent_repeat(self):
        c=small_config();s=InnerSearch(self.X,self.y,np.arange(16),self.specs,[3]*36,c,time.monotonic()+15,self.path,lambda x:None)
        candidate={'model':'baseline','parameters':{},'view':'all','target_transform':'identity'}
        p=s.oof_repeated(candidate,0);self.assertEqual(p.shape,(32,))
        self.assertFalse(np.allclose(p[:16],p[16:]));np.testing.assert_allclose(s.oof(candidate,0),p.reshape(2,16).mean(axis=0))
    def test_correlated_fields_share_stability_group_and_fold_only_statistics(self):
        X=self.X.copy();X[:,1]=X[:,0]
        p=PricePreparation(self.specs,[3]*36,strength=0.,view='screened',select_k=1,screening='nonlinear',stability_resamples=24).fit(X,self.y)
        selected=p.selection_details_['selected_fields'];self.assertEqual(selected,['x0'])
        groups=p.selection_details_['stability']['groups'];group=next(g for g in groups if 'x0' in g['field_ids'])
        self.assertIn('x1',group['field_ids']);self.assertGreaterEqual(group['selection_frequency'],.8)
        means=copy.deepcopy(p.parts);p.transform(np.full_like(X,100000.));self.assertEqual(means,p.parts)
    def test_constant_and_categorical_only_stability_is_safe(self):
        p=PricePreparation(self.specs,[3]*36,strength=0.,view='screened',select_k=2,stability_resamples=8).fit(np.ones_like(self.X),self.y)
        self.assertEqual(p.transform(self.X).shape,(16,1));self.assertTrue(np.all(p.transform(self.X)==0))
        specs=[dict(self.specs[0],value_type='enum')];X=np.arange(16).reshape(-1,1)%3
        p=PricePreparation(specs,[3],view='screened',select_k=1,stability_resamples=8).fit(X,self.y)
        self.assertTrue(np.all(np.isfinite(p.transform(X))))
    def test_bad_model_gets_zero_weight_without_forced_member(self):
        c=profile();bank=np.column_stack([self.y*1.01,self.y*2.,self.y*3.])
        indices,w,p=robust_blend(self.y,bank,c)
        self.assertEqual(indices,[0]);np.testing.assert_array_equal(w,[1.]);np.testing.assert_allclose(p,bank[:,0])
        self.assertLess(len(indices),c['ensemble']['min_members'])
        # A spectacularly bad model can cancel a small bias with 1% weight;
        # it still fails the candidate reliability filter.
        indices,w,p=robust_blend(self.y,np.column_stack([self.y*.97,self.y*4.]),c)
        self.assertEqual(indices,[0])
    def test_complementary_members_improve_without_degrading_anchor(self):
        c=profile();bank=np.column_stack([self.y*.97,self.y*1.04,self.y*4.])
        indices,w,p=robust_blend(self.y,bank,c)
        self.assertEqual(set(indices),{0,1});self.assertAlmostEqual(float(w.sum()),1.)
        self.assertLess(score(self.y,p)['mape_percent'],.1)
        self.assertGreater(score(self.y,p)['r2'],score(self.y,bank[:,0])['r2'])
    def test_repeat_variation_is_penalized(self):
        y=np.tile(self.y,2);p=np.r_[self.y*.99,self.y*1.2]
        self.assertGreater(risk_loss(y,p,.7,2,1.),risk_loss(y,p,.7,2,0.))
    def test_bayesian_candidates_round_trip_and_explain(self):
        for name in ('bayesian_ridge','ard'):
            cfg={'model':name,'parameters':{},'view':'screened','select_k':3,'expert_strength':0.,'stability_resamples':8}
            m=R4PriceRegressor(self.specs,[3]*36,cfg).fit(self.X,self.y)
            self.assertTrue(np.all(np.isfinite(m.predict(self.X))))
            np.testing.assert_allclose(m.predict(self.X),pickle.loads(pickle.dumps(m,protocol=4)).predict(self.X))
            explanation=m.explanation(self.X[:1]);self.assertTrue(explanation['attribute_terms'])
            self.assertAlmostEqual(sum(v['term'] for v in explanation['attribute_terms'])+explanation['intercept'],m.predict(self.X[:1])[0])
    def test_trust_region_flags_joint_and_individual_extrapolation(self):
        region=PriceTrustRegion().fit(self.X,self.specs,self.y)
        self.assertFalse(region.assess(self.X[:1])[0]['is_extrapolation'])
        p=np.ones((1,36))*20;r=region.assess(p)[0]
        self.assertTrue(r['is_extrapolation']);self.assertEqual(len(r['outside_fields']),36)
        b={'price_trust_region':region,'price_risk_policy':{'extrapolation_penalty':.1},'target_divisor_to_wan':1.}
        result=price_risk(b,p,[{'price_native':.001,'weight':1.}],.001)
        self.assertGreater(result['recommendation_price_wan'],50.)
        self.assertIn('not a calibrated',result['interpretation'])
    def test_recommendation_uses_same_conservative_price_for_budget_and_order(self):
        a={'agreement_id':'risky','predicted_price_wan':1.,'recommendation_price_wan':150.,'capability_score':90.,'conservative_capability_score':80.,'feasibility_probability':.99}
        b=dict(a,agreement_id='safe',predicted_price_wan=100.,recommendation_price_wan=100.)
        self.assertFalse(agreement_matches(a,{'max_price':120}));self.assertTrue(agreement_matches(b,{'max_price':120}))
        r=rank_agreements([a,b],{'sort_by':'price'},{});self.assertEqual(r[0]['agreement_id'],'safe')
        self.assertEqual(r[1]['predicted_price_wan'],1.);self.assertAlmostEqual(r[1]['cost_effectiveness'],round(80./150.,3))
        self.assertEqual(effective_price(dict(a,recommendation_price_wan=.5)),1.)
    def test_influence_reports_all_records_without_changing_input(self):
        from tools.product_r43_diagnostics import influence_report
        original=self.X.copy();r=influence_report(self.X,self.y,np.arange(16),self.specs,[str(i) for i in range(16)],profile()['quality'])
        self.assertEqual(len(r['records']),16);np.testing.assert_array_equal(self.X,original)
        self.assertTrue(all(np.isfinite(row['other_records_prediction_drift_percent']) for row in r['records']))
    def test_gateway_and_candidate_generators_share_conservative_price(self):
        from app.model_service_client import ModelServiceGateway
        from app.local_generator import HistorySeededGenerator
        from app.gflownet_generator import TabularTrajectoryBalanceGFlowNet
        price={'prediction':{'predicted_price_wan':1.,'recommendation_price_wan':150.,'price_risk':{'is_extrapolation':True}}}
        effect={'evaluation':{'effectiveness_score':90.,'conservative_capability_score':80.,'feasibility_probability':.99}}
        result=ModelServiceGateway._merge({'parameters':{}},price,effect)
        self.assertEqual(result['recommendation_price_wan'],150.);self.assertEqual(result['predicted_price_wan'],1.)
        self.assertAlmostEqual(result['cost_effectiveness'],round(80./150.,4))
        risky=dict(result);nominal=dict(result,recommendation_price_wan=1.,cost_effectiveness=80.)
        generator=object.__new__(HistorySeededGenerator)
        self.assertLess(generator._objective_value(risky),generator._objective_value(nominal))
        self.assertLess(TabularTrajectoryBalanceGFlowNet._log_reward(risky,100.),TabularTrajectoryBalanceGFlowNet._log_reward(nominal,100.))
    def test_installer_preserves_targets_scores_and_saved_state(self):
        from tools.upgrade_product_r43 import upgrade
        c=self.path/'config';c.mkdir();old=load_config(overrides={'quality':{'mape_percent_max':5.,'hard_gate':True},'expert':{'scores':{'x0':5}}})
        (c/'price_automl_r4.json').write_text(json.dumps(old),encoding='utf-8');(c/'portable.json').write_text('{}',encoding='utf-8')
        state=self.path/'saved_model.pkl';state.write_bytes(b'original model');new=upgrade(self.path,'data_only')
        self.assertEqual(old['quality'],new['quality']);self.assertEqual(old['expert']['scores'],new['expert']['scores'])
        self.assertFalse(new['expert']['enabled']);self.assertTrue(new['ensemble']['robust']);self.assertEqual(state.read_bytes(),b'original model')
        self.assertEqual(len(list(c.glob('price_automl_r4.before_r43_*'))),1)
    def test_profiles_validate_and_retain_5000_seed_cap(self):
        for name in ('data_only','quick','expert_compare','loo_audit'):
            c=profile(name);self.assertEqual(c['seed_search']['max_trials'],5000);self.assertEqual(c['quality']['mape_percent_max'],10.)
            self.assertEqual(c['expert']['enabled'],name=='expert_compare');self.assertTrue(c['trust_region']['enabled'])
    def test_full_16_by_36_training_independent_audit_and_risk_round_trip(self):
        out=self.path/'train'
        with threadpool_limits(limits=1):bundle,r=train_price_r4(copy.deepcopy(self.data),out,overrides=small_config(),progress=lambda x:None)
        self.assertTrue(r['audit_complete']);self.assertEqual(r['training_rows'],16);self.assertEqual(r['feature_count'],36)
        self.assertTrue(r['search_diagnostics']['coverage']['family_first']);self.assertEqual(r['search_diagnostics']['coverage']['unattempted_models'],[])
        rows=json.loads((out/'nested_audit.json').read_text(encoding='utf-8'))['records']
        pred=np.asarray([row['predictions_by_repetition'] for row in rows]).T.reshape(-1)
        expected=score(np.tile(self.y,2),pred);self.assertAlmostEqual(expected['mape_percent'],r['nested_cv_metrics']['mean_mape'])
        self.assertAlmostEqual(expected['r2'],r['nested_cv_metrics']['mean_r2'])
        self.assertEqual(len(r['outer_repetitions']['repetitions']),2)
        self.assertTrue((out/'sample_influence.json').is_file());self.assertTrue((out/'feature_stability.json').is_file())
        saved=load_bundle(out/'price_native_bundle.pkl');items=[row['params'] for row in self.data['agreements'][:2]]
        single=[predict(saved,p) for p in items];batch=predict_batch(saved,items)
        for a,b in zip(single,batch):
            self.assertEqual(a['price_risk'],b['price_risk']);self.assertAlmostEqual(a['predicted_price_wan'],b['predicted_price_wan'])
            self.assertGreaterEqual(a['recommendation_price_wan'],a['predicted_price_wan'])


if __name__=='__main__':unittest.main(verbosity=2)
