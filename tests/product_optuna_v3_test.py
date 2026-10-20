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
import pandas as pd
from threadpoolctl import threadpool_limits
from product_optuna_v2_test import fixture
from tools.product_price_optuna_v2 import train_price_optuna_v2,load_config,dataset
from tools.product_price_optuna_v3 import train_price_optuna_v3,load_config as v3_config,search_settings
from services.price_service.optuna_v2 import price_automl_core as core
from services.price_service.optuna_v2.typed_features import FeatureEncoder,TypedBackend,apply_types
from services.price_service.optuna_v2.cached_backend import CachedBackend
from services.price_service.optuna_v2.optimizer import _original_baseline
from services.price_service.optuna_v2.optimizer_v3 import LocalTrial
from services.price_service.optuna_v2.search_history import read_history
from services.price_service.native_bundle import predict,predict_batch,load_bundle
from services.price_service.app import PriceService


def mixed_fixture(n=16,p=36):
    _,_,_,data=fixture(n,p);rng=np.random.RandomState(611)
    for i,row in enumerate(data['agreements']):
        row['params'].update(x0=int(1+i%5),x1=float(20+60*rng.rand()),x2=float(1800+5400*rng.rand()),x3=('液压','电动','气动')[i%3],x4=(10,40,90)[(i//2)%3])
        v=row['params'];row['historical_price_wan']=float(np.exp(3+.06*v['x0']+.003*v['x1']+.00002*v['x2']+.12*(v['x3']=='液压')+rng.normal(0,.015)))
    data['parameters'][3].update(value_type='enum',allowed_values_json=json.dumps(['液压','电动','气动'],ensure_ascii=False))
    return data


class OptunaV3Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holder=tempfile.TemporaryDirectory();cls.path=Path(cls.holder.name);cls.data=mixed_fixture()
        cls.shared={'expert':{'scores':{'x0':5,'x4':4},'field_types':{'x0':'数值','x3':'配置类型','x4':'configuration'}}}
        with threadpool_limits(limits=1):
            cls.v2,cls.r2=train_price_optuna_v2(cls.data,cls.path/'v2',overrides=dict(cls.shared,search={'n_trials':3}),progress=lambda s:None)
            cls.v3,cls.r3=train_price_optuna_v3(cls.data,cls.path/'v3',overrides=dict(cls.shared,search={'n_trials':6},v3={'startup_trials':1}),progress=lambda s:None)
    @classmethod
    def tearDownClass(cls):cls.holder.cleanup()
    def test_only_two_semantic_types_and_numeric_integer_scales(self):
        data=copy.deepcopy(self.data);apply_types(data,load_config(overrides=self.shared)['expert']['field_types'])
        self.assertEqual(set(p['value_type'] for p in data['parameters']),{'number','enum'})
        self.assertEqual(data['parameters'][0]['value_type'],'number');self.assertEqual(data['parameters'][4]['value_type'],'enum')
        self.assertIsInstance(data['agreements'][0]['params']['x0'],float)
        self.assertEqual(self.r3['attribute_types']['x4'],'配置类型')
        with self.assertRaises(ValueError):load_config(overrides={'expert':{'field_types':{'x0':'ordinal'}}})
    def test_invalid_validation_and_constant_prices_fail_before_search(self):
        with self.assertRaisesRegex(ValueError,'至少需要2条'):dataset(self.data,load_config(overrides={'validation':{'test_size':.01,'auto_adjust_test_size':False}}))
        data=copy.deepcopy(self.data)
        for row in data['agreements']:row['historical_price_wan']=100.
        with self.assertRaisesRegex(ValueError,'完全相同'):dataset(data,load_config())
    def test_declared_configuration_preserves_leading_zero_and_ip_tokens(self):
        import csv,io
        from app.product_table import product_table_rows
        from app.historical_onboarding import HistoricalProductOnboarding
        from services.price_service.optuna_v2.typed_features import restore_configurations
        lines=[['成品编号','成品名称','价格(万元)','安装代码','接口配置'],['product_id','product_name','price_wan','mount','interface']]
        lines.extend(['A%d'%i,'样例',10+i,('001','1')[i%2],('IP65','IP67')[i%2]] for i in range(8))
        stream=io.StringIO(newline='');csv.writer(stream).writerows(lines);raw=stream.getvalue().encode('utf-8-sig')
        report=HistoricalProductOnboarding().analyze('test.csv',raw,'TEST','样例',[],require_dual_header=True)
        cfg=load_config(overrides={'expert':{'field_types':{'mount':'配置类型','interface':'配置类型'}}})
        restore_configurations(report['data'],product_table_rows('test.csv',raw,require_dual_header=True),cfg['expert']['field_types'],[])
        frame,y,specs,mappings,*_=dataset(report['data'],cfg)
        self.assertEqual(set(mappings['mount']),{'001','1'});self.assertEqual(set(mappings['interface']),{'IP65','IP67'})
        self.assertNotEqual(frame.loc[0,'mount'],frame.loc[1,'mount'])
    def test_v2_and_v3_share_identical_input_protocol(self):
        self.assertEqual(self.r2['search_identity'],self.r3['search_identity'])
        self.assertEqual(self.r2['feature_order'],self.r3['feature_order']);self.assertEqual(self.r3['feature_count'],36)
        self.assertGreater(self.r3['encoded_feature_count'],36)
    def test_v3_runs_both_phases_with_one_shared_budget(self):
        self.assertEqual(self.r3['phase_counts'],{'nsga':4,'tpe':2});self.assertEqual(self.r3['trials_completed'],6)
        self.assertEqual(self.r3['tpe_inherited_completed_trials'],4);self.assertEqual(self.r3['candidate_pool_count'],6)
        self.assertEqual(self.r3['history_trials_imported'],0)
    def test_best_candidate_is_selected_from_union_of_both_phases(self):
        trials=pd.read_csv(self.path/'v3/all_trials.csv');pareto=pd.read_csv(self.path/'v3/pareto_front.csv')
        self.assertEqual(set(trials.phase),{'nsga','tpe'});self.assertTrue((pareto['trial']==self.r3['selected_trial']).any())
        selected=trials[trials.trial==self.r3['selected_trial']].iloc[0]
        self.assertAlmostEqual(selected.r2,self.r3['validation_metrics']['r2'],places=10)
        self.assertAlmostEqual(selected.mape,self.r3['validation_metrics']['mape'],places=10)
    def test_onehot_has_no_numeric_order_and_unseen_category_does_not_leak(self):
        frame=pd.DataFrame({'config':[10.,90.,10.],'quantity':[1.,5.,3.]})
        encoder=FeatureEncoder(['config']).fit(frame);encoded=encoder.transform(frame)
        np.testing.assert_array_equal(encoded[:,:2],[[1,0],[0,1],[1,0]])
        np.testing.assert_array_equal(encoder.transform(pd.DataFrame({'config':[40.],'quantity':[8.]})),[[0,0,8]])
        self.assertNotIn(40.,encoder.categories['config'])
        renamed=frame.copy();renamed['config']=frame['config'].map({10.:7200.,90.:1.})
        other=FeatureEncoder(['config']).fit(renamed).transform(renamed)
        np.testing.assert_array_equal(encoded[:,:2],other[:,:2][:,::-1])
    def test_native_price_matches_original_wrapper_with_categorical_inputs(self):
        params=[r['params'] for r in self.data['agreements']]
        frame,*_=dataset(self.data,load_config(overrides=self.shared))
        for engine in ('v2','v3'):
            fitted=core.load_bundle(str(self.path/engine/'price_ensemble_bundle.pkl'))
            expected,_=TypedBackend.predict(fitted,frame);native=load_bundle(self.path/engine/'price_native_bundle.pkl')
            batch=predict_batch(native,params)
            np.testing.assert_allclose([r['predicted_price_wan'] for r in batch],expected,atol=1e-6)
            self.assertEqual(predict(native,params[0]),batch[0])
        self.assertTrue(self.r3['selected_split_reload_verified'])
    def test_chinese_explanation_retains_original_attribute_owner_and_expert_score(self):
        service=PriceService(self.path/'v3/price_native_bundle.pkl')
        result=service.handle_post('/api/v1/predict',{'parameters':self.data['agreements'][0]['params']})
        explanation=result['explanation'];self.assertEqual(len(explanation['price_attribute_scores']),36)
        terms=[t for d in explanation['configuration_details'] for m in d['members'] for t in m.get('attribute_terms',[]) if t.get('encoding')=='one_hot']
        self.assertTrue(terms);self.assertTrue(all(t['field_id'] in ('x3','x4') and '价格属性' in t['label'] for t in terms))
        native=core.load_bundle(str(self.path/'v3/price_ensemble_bundle.pkl'))
        self.assertTrue(all(native['expert_map'][name]==4 for name,owner in native['input_encoder'].owners.items() if owner=='x4'))
    def test_weight_only_changes_reuse_predictions_without_stale_inactive_models(self):
        frame,y,_,mappings,_,_=dataset(self.data,load_config(overrides=self.shared));rows=np.arange(3,19)
        backend=CachedBackend(tuple(mappings),cache_entries=32);backend.searching=True;params=_original_baseline(7527)
        expert={'x0':5.,'x4':4.}
        with threadpool_limits(limits=1):
            a=backend.evaluate(params,frame,y,rows,expert,.2,3)[0];fits=backend.stats['model_fits']
            altered=dict(params,weight_lasso=.9,weight_ridge=.2)
            b=backend.evaluate(altered,frame,y,rows,expert,.2,3)[0]
            self.assertEqual(backend.stats['model_fits'],fits);self.assertEqual(backend.stats['prediction_cache_hits'],6)
            expected=TypedBackend(tuple(mappings)).evaluate(altered,frame,y,rows,expert,.2,3)[0]
        self.assertAlmostEqual(b['mape'],expected['mape'],places=12)
        self.assertNotEqual(a['mape'],b['mape'])
        with threadpool_limits(limits=1):backend.evaluate(altered,frame,y,rows,expert,.3,3)
        self.assertGreater(backend.stats['model_fits'],fits)
    def test_cache_does_not_treat_inactive_zero_as_fitted_prediction(self):
        frame,y,_,mappings,_,_=dataset(self.data,load_config(overrides=self.shared));rows=np.arange(3,19)
        backend=CachedBackend(tuple(mappings));backend.searching=True;params=_original_baseline(7527)
        for name in core.MODEL_NAMES:params['weight_'+name]=0.
        params['weight_ridge']=1.
        with threadpool_limits(limits=1):
            backend.evaluate(params,frame,y,rows,{},.2,3);self.assertEqual(backend.stats['model_fits'],1)
            params['weight_gbdt']=.5;actual=backend.evaluate(params,frame,y,rows,{},.2,3)[0]
            expected=TypedBackend(tuple(mappings)).evaluate(params,frame,y,rows,{},.2,3)[0]
        self.assertEqual(backend.stats['model_fits'],2);self.assertAlmostEqual(actual['mape'],expected['mape'],places=12)
    def test_same_history_reuses_observations_changed_data_reevaluates_parameters(self):
        history=self.path/'v2/study_history.json';identity=self.r2['search_identity']
        same,proposals,details=read_history(history,identity);self.assertEqual(len(same),3);self.assertFalse(proposals)
        changed,proposals,details=read_history(history,'changed-price');self.assertFalse(changed);self.assertEqual(len(proposals),3)
        self.assertEqual(details['policy'],'parameters_only_reevaluate')
        forced,proposals,details=read_history(history,identity,reuse_metrics=False);self.assertFalse(forced);self.assertEqual(len(proposals),3)
    def test_v3_warm_history_increases_pool_but_not_new_budget(self):
        overrides=copy.deepcopy(self.shared);overrides.update(search={'n_trials':2},v3={'startup_trials':1,'warm_start':{'history_file':str(self.path/'v2/study_history.json')}})
        with threadpool_limits(limits=1):_,report=train_price_optuna_v3(self.data,self.path/'warm',overrides=overrides,progress=lambda s:None)
        self.assertEqual(report['history_trials_imported'],3);self.assertEqual(report['trials_completed'],2)
        self.assertEqual(report['candidate_pool_count'],5);self.assertEqual(report['phase_counts'],{'nsga':1,'tpe':1})
    def test_local_refinement_fixes_seed_and_preserves_categorical_space(self):
        import optuna
        study=optuna.create_study();trial=study.ask();anchor=_original_baseline(9300)
        cfg=core.model_params_from_trial(LocalTrial(trial,anchor,.25),1,10000)
        self.assertEqual(cfg['split_seed'],9300);self.assertEqual(trial.distributions['split_seed'].low,9300)
        self.assertEqual(trial.distributions['svr_kernel'].choices,('linear','rbf'))
    def test_seed_escape_supports_full_unsigned_32_bit_range_on_windows_numpy(self):
        import optuna
        from services.price_service.optuna_v2.optimizer_v3 import SeedIndependentTPE
        sampler=SeedIndependentTPE(42,1,24)
        distribution=optuna.distributions.IntDistribution(2**31,2**32-1)
        value=sampler.sample_independent(None,None,'split_seed',distribution)
        self.assertTrue(2**31<=value<=2**32-1)
    def test_v3_works_on_ten_records_forty_features(self):
        data=mixed_fixture(10,40)
        with threadpool_limits(limits=1):_,report=train_price_optuna_v3(data,self.path/'10x40',overrides={'search':{'n_trials':3},'expert':{'field_types':{'x4':'配置类型'}},'v3':{'startup_trials':1}},progress=lambda s:None)
        self.assertEqual(report['training_rows'],10);self.assertEqual(report['feature_count'],40);self.assertEqual(report['validation_test_rows'],2)
        self.assertLess(report['native_equivalence_max_delta_wan'],1e-5)
    def test_units_change_preserves_predictions(self):
        frame,y,*_=dataset(self.data,load_config(overrides=self.shared));numerics=frame.drop(columns=['x3','x4'])
        converted=numerics.copy();converted['x2']/=3600.;params=_original_baseline(7527)
        with threadpool_limits(limits=1):
            a=core.predict_bundle(core.fit_ensemble(numerics,y,params,{}),numerics)[0]
            b=core.predict_bundle(core.fit_ensemble(converted,y,params,{}),converted)[0]
        np.testing.assert_allclose(a,b,rtol=1e-10,atol=1e-10)
    def test_saved_engine_choice_requires_its_own_model_and_preserves_both(self):
        from tools.product_optuna_versions import select
        from tools import product_portable as portable
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'config').mkdir();(root/'state').mkdir()
            (root/'config/portable.json').write_text(json.dumps({'price_engine':'optuna_v2'}),encoding='utf-8')
            for name in ('optuna_v2','optuna_v3'):(root/('config/price_'+name+'.json')).write_text('{}')
            for engine in ('optuna_v2','optuna_v3'):
                output=root/'outputs'/engine;output.mkdir(parents=True);manifest={'status':'ready','price_engine':engine,'artifact_sha256':{}}
                for key in ('price_model','effectiveness_model','business_package'):
                    (output/key).write_bytes(engine.encode());manifest[key]=key;manifest['artifact_sha256'][key]=hashlib.sha256(engine.encode()).hexdigest()
                (output/'prototype_manifest.json').write_text(json.dumps(manifest))
                (root/'state'/('latest_'+engine+'.json')).write_text(json.dumps({'output':'outputs/'+engine}))
            original=portable.ROOT
            try:
                portable.ROOT=root
                select(root,'optuna_v3');self.assertEqual(portable.latest_output(),root/'outputs/optuna_v3')
                select(root,'optuna_v2');self.assertEqual(portable.latest_output(),root/'outputs/optuna_v2')
                (root/'state/latest_optuna_v3.json').unlink();select(root,'optuna_v3');self.assertIsNone(portable.latest_output())
                self.assertTrue((root/'outputs/optuna_v2/price_model').exists())
            finally:portable.ROOT=original

if __name__=='__main__':unittest.main(verbosity=2)
