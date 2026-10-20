# -*- coding: utf-8 -*-
import copy
import hashlib
import json
import math
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
from product_optuna_v2_test import fixture
from product_optuna_v3_test import mixed_fixture
from tools.product_price_optuna_v4 import load_config,train_price_optuna_v4
from tools.product_price_optuna_v2 import dataset
from services.price_service.optuna_v4.splits import make_splits,holdout_indices,coverage_subset
from services.price_service.optuna_v4.metrics import metrics,summarize,ranking
from services.price_service.optuna_v4.backend import Backend,mix_evaluations
from services.price_service.optuna_v4.parameters import baselines,space_limits
from services.price_service.optuna_v4.optimizer import budget_plan
from services.price_service.native_bundle import predict,predict_batch
from tools.product_optuna_v4_adapter import groups_for


class OptunaV4Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holder=tempfile.TemporaryDirectory();cls.path=Path(cls.holder.name);cls.data=mixed_fixture(7,40)
        cls.options={'search':{'n_trials':36},'expert':{'field_types':{'x3':'enum','x4':'enum'}},
                     'v4':{'review':{'max_candidates':3},'ensemble':{'weight_trials':4}}}
        cls.bundle,cls.report=train_price_optuna_v4(cls.data,cls.path/'mixed',overrides=cls.options,progress=lambda s:None)
    @classmethod
    def tearDownClass(cls):cls.holder.cleanup()
    def test_all_small_sample_unique_partitions(self):
        cfg=load_config()
        for n,expected in ((4,6),(5,10),(6,15),(7,21)):
            parts,report=make_splits(np.arange(n)+1.,np.arange(n),cfg)
            self.assertEqual(len(parts),expected);self.assertTrue(report['exhaustive'])
            self.assertEqual(len({tuple(f['validation']) for f in parts}),expected)
            for f in parts:self.assertFalse(set(f['train'])&set(f['validation']));self.assertEqual(sorted(f['train']),list(f['train']))
    def test_enumeration_cap_and_explicit_exhaustive(self):
        cfg=load_config(overrides={'v4':{'splits':{'max_splits':3}}});parts,report=make_splits(np.arange(7)+1.,np.arange(7),cfg)
        self.assertEqual(len(parts),3);self.assertFalse(report['exhaustive'])
        cfg['v4']['splits']['coverage']='exhaustive';self.assertEqual(len(make_splits(np.arange(7)+1.,np.arange(7),cfg)[0]),21)
    def test_budget_is_shared_or_explicitly_per_partition(self):
        cfg=load_config(overrides={'search':{'n_trials':100}});plan=budget_plan(cfg,21)
        self.assertEqual(plan['total_configuration_trials'],100);self.assertEqual(sum(plan['per_split_trials']),60)
        cfg['v4']['search'].update(budget_mode='per_split',trials_per_split=7,global_trials=11)
        self.assertEqual(budget_plan(cfg,21)['total_configuration_trials'],158)
        cfg['v4']['search']['budget_mode']='total';cfg['search']['n_trials']=10
        with self.assertRaises(ValueError):budget_plan(cfg,21)
    def test_training_gate_uses_full_refit_as_well(self):
        s=copy.deepcopy(self.report['cross_split_summary']);cfg=load_config()
        good=copy.deepcopy(s);good.update(train_violation=0.,deployed_train_violation=0.)
        bad=copy.deepcopy(good);bad['deployed_train_violation']=1.
        self.assertLess(ranking(good,cfg),ranking(bad,cfg))
        self.assertIn('full_refit_training_metrics',self.report)
        if self.report['targets_met']:self.assertTrue(self.report['deployed_training_gate_passed'])
    def test_train_gate_scopes_and_undefined_r2(self):
        y=np.array([1.,1.,2.,4.]);folds=[{'id':0,'train':np.array([0,1]),'validation':np.array([2,3]),'train_prediction':np.array([1.,1.]),'validation_prediction':np.array([2.,4.])}]
        cfg=load_config(overrides={'v4':{'train_gate':{'scope':'every_split'}}});s=summarize(folds,y,cfg)
        self.assertIsNone(s['train']['r2']);self.assertGreater(s['train_violation'],0);self.assertEqual(s['undefined_train_r2_folds'],1)
        cfg['v4']['train_gate']['r2_min']=None;self.assertEqual(summarize(folds,y,cfg)['train_violation'],0)
    def test_r2_is_pooled_predictions_not_average_fold_r2(self):
        y=np.array([1.,2.,10.,20.]);folds=[{'id':0,'train':np.array([2,3]),'validation':np.array([0,1]),'train_prediction':np.array([10.,20.]),'validation_prediction':np.array([1.1,2.1])},
         {'id':1,'train':np.array([0,1]),'validation':np.array([2,3]),'train_prediction':np.array([1.,2.]),'validation_prediction':np.array([9.,19.])}]
        s=summarize(folds,y,load_config());expected=metrics(y,np.array([1.1,2.1,9.,19.]))
        self.assertAlmostEqual(s['validation']['r2'],expected['r2']);self.assertNotAlmostEqual(s['validation']['r2'],np.mean([m['validation']['r2'] for m in s['fold_metrics']]))
    def test_nonfinite_candidates_are_rejected(self):
        for predicted in ([1.,float('inf')],[float('nan'),2.],[1e304,1e304]):
            with self.assertRaises(ValueError):metrics(np.array([1.,2.]),predicted)
    def test_identical_inputs_and_batch_groups_form_union(self):
        data=copy.deepcopy(self.data);data['agreements'][1]['params']=dict(data['agreements'][0]['params'])
        data['agreements'][2]['params']['x0']=data['agreements'][1]['params']['x0']
        cfg=load_config(overrides={'expert':{'field_types':{'x3':'enum','x4':'enum'}},'v4':{'splits':{'group_field':'x0'}}})
        frame,_,_,_,_,ids=dataset(data,cfg);groups=groups_for(frame,ids,data,cfg)
        self.assertEqual(groups[0],groups[1]);self.assertEqual(groups[1],groups[2])
        splits,_=make_splits(np.arange(7)+1.,groups,cfg)
        for f in splits:self.assertFalse(set(groups[f['train']])&set(groups[f['validation']]))
    def test_locked_holdout_counts_and_type_requirement(self):
        cfg=load_config(overrides={'v4':{'evaluation':{'mode':'locked_holdout'}}})
        with self.assertRaises(ValueError):holdout_indices(4,['A','B','C','D'],np.arange(4),cfg)
        with self.assertRaisesRegex(ValueError,'声明'):
            train_price_optuna_v4(self.data,self.path/'undeclared',overrides={'v4':{'evaluation':{'mode':'locked_holdout'}}},progress=lambda s:None)
    def test_cached_train_and_validation_predictions_match_refit(self):
        cfg=load_config(overrides=self.options);frame,y,_,mappings,_,_=dataset(self.data,cfg);folds,_=make_splits(y,np.arange(7),cfg)
        backend=Backend(frame,y,np.arange(7),{c:1. for c in frame.columns},tuple(mappings))
        params=baselines(space_limits(5,True),[0])[0];first=backend.evaluate(params,folds[:1],cfg)
        fitted=backend.fit(frame.iloc[folds[0]['train']],y[folds[0]['train']],params,backend.expert_map)
        a,_=backend.predict(fitted,frame.iloc[folds[0]['train']]);b,_=backend.predict(fitted,frame.iloc[folds[0]['validation']])
        np.testing.assert_allclose(a,first['folds'][0]['train_prediction']);np.testing.assert_allclose(b,first['folds'][0]['validation_prediction'])
        count=backend.stats['model_fits'];backend.evaluate(params,folds[:1],cfg)
        self.assertEqual(backend.stats['model_fits'],count);self.assertGreater(backend.stats['prediction_cache_hits'],0)
        changed=dict(params,model_seed=9997);backend.evaluate(changed,folds[:1],cfg)
        self.assertEqual(backend.stats['model_fits'],count)
    def test_encoder_and_scaler_fit_only_training_rows(self):
        cfg=load_config(overrides=self.options);frame,y,_,maps,_,_=dataset(self.data,cfg)
        backend=Backend(frame,y,np.arange(7),{c:1. for c in frame.columns},tuple(maps));f={'id':0,'train':np.array([0,1]),'validation':np.array([2,3,4,5,6])}
        a,b,experts=backend.prepare(f)
        self.assertEqual(len(a),2);self.assertEqual(len(b),5)
        self.assertTrue(np.allclose(a.min().values,0));self.assertTrue(np.all((a.max().values==0)|(np.isclose(a.max().values,1))))
        self.assertEqual(len([c for c in a.columns if c.startswith('__price_category_3_')]),2)
    def test_misaligned_folds_cannot_be_ensembled(self):
        y=np.arange(4)+1.;a={'folds':[{'id':0,'train':np.array([0,1]),'validation':np.array([2,3]),'train_prediction':y[:2],'validation_prediction':y[2:]}]};b=copy.deepcopy(a);b['folds'][0]['validation']=np.array([3,2])
        with self.assertRaises(ValueError):mix_evaluations([a,b],[.5,.5],y,load_config())
    def test_export_single_batch_and_explanation(self):
        rows=[r['params'] for r in self.data['agreements']];batch=predict_batch(self.bundle,rows)
        for p,r in zip(batch,rows):self.assertEqual(p['predicted_price_wan'],predict(self.bundle,r)['predicted_price_wan'])
        explained=next(iter(self.bundle['models'].values())).explanation(np.array([[float(self.data['agreements'][0]['params'][c]) if c not in ('x3','x4') else 0. for c in self.report['feature_order']]]))
        self.assertTrue(explained['members']);self.assertAlmostEqual(sum(m['weight'] for m in explained['members']),1.)
        self.assertLess(self.report['native_equivalence_max_delta_wan'],1e-5)
        from services.price_service.app import PriceService
        response=PriceService(self.path/'mixed/price_native_bundle.pkl').handle_post('/api/v1/predict',{'parameters':rows[0]})
        explanation=response['explanation'];total=sum(d['ensemble_weight']*m['weight']*m['predicted_price_wan'] for d in explanation['configuration_details'] for m in d['members'])
        self.assertAlmostEqual(total,response['prediction']['predicted_price_wan'],places=5);self.assertIn('training_metrics',explanation)
    def test_seed_range_is_not_limited_to_three_initial_seeds(self):
        diagnostic=json.loads((self.path/'mixed/search_diagnostics.json').read_text(encoding='utf-8'))
        seeds={r['parameters']['model_seed'] for r in diagnostic['records'] if r['valid']}
        self.assertTrue(seeds-set([0,42,137]));self.assertTrue(all(0<=x<=10000 for x in seeds))
    def test_constrained_sampler_across_multiple_nsga_generations_and_tpe(self):
        import optuna
        import warnings
        from services.price_service.optuna_v4.optimizer import ModelSeedNSGA,ModelSeedTPE,ModelLocalTrial
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',optuna.exceptions.ExperimentalWarning)
            first=optuna.create_study(directions=['minimize','minimize'],sampler=ModelSeedNSGA(seed=42,population_size=4,constraints_func=lambda t:(t.user_attrs.get('train_violation',1.),)))
            second=optuna.create_study(directions=['minimize','minimize'],sampler=ModelSeedTPE(42,3,12,constraints_func=lambda t:(t.user_attrs.get('train_violation',1.),)))
        for i in range(20):
            trial=first.ask();seed=trial.suggest_int('model_seed',0,10000);x=trial.suggest_float('alpha',1e-8,1e4,log=True)
            trial.set_user_attr('train_violation',float(x>100));first.tell(trial,(abs(math.log(x)),abs(seed-123)/10000.))
        second.add_trials(first.trials)
        for i in range(8):
            trial=second.ask();proxy=ModelLocalTrial(trial,{'model_seed':0,'alpha':1.},.25)
            seed=proxy.suggest_int('model_seed',0,10000);x=proxy.suggest_float('alpha',1e-8,1e4,log=True)
            self.assertEqual(trial.distributions['model_seed'].high,10000)
            trial.set_user_attr('train_violation',0.);second.tell(trial,(abs(math.log(x)),abs(seed-123)/10000.))
        self.assertEqual(len(second.trials),28)
    def test_no_business_accuracy_claim_and_training_budget_exact(self):
        self.assertFalse(self.report['business_accuracy_certified']);self.assertEqual(self.report['trials_completed'],36)
        self.assertEqual(self.report['split_plan']['evaluated_splits'],21);self.assertEqual(self.report['attribute_types']['x4'],'配置类型')
        self.assertEqual(self.report['holdout_metrics']['scope'],'external_test_not_provided')
    def test_all_four_search_modes_and_smallest_dataset(self):
        for mode in ('random','global','per_split','hybrid'):
            with self.subTest(mode=mode):
                _,r=train_price_optuna_v4(fixture(4,40)[-1],self.path/mode,overrides={'search':{'n_trials':12},'v4':{'search':{'mode':mode},'review':{'max_candidates':2},'ensemble':{'weight_trials':2}}},progress=lambda s:None)
                self.assertEqual(r['search_mode'],mode);self.assertEqual(r['trials_completed'],12);self.assertLess(r['native_equivalence_max_delta_wan'],1e-5)
    def test_outer_labels_do_not_change_selected_parameters_or_predictor(self):
        data=mixed_fixture(6,40);types={p['parameter_id']:('enum' if p['parameter_id'] in ('x3','x4') else 'number') for p in data['parameters']}
        opts={'search':{'n_trials':4},'expert':{'field_types':types},'v4':{'search':{'mode':'global'},'review':{'max_candidates':2},'ensemble':{'weight_trials':2},'evaluation':{'mode':'locked_holdout','holdout_ids':['O004','O005']}}}
        b,a=train_price_optuna_v4(data,self.path/'outer_a',overrides=opts,progress=lambda s:None)
        changed=copy.deepcopy(data);changed['agreements'][4]['historical_price_wan']*=100.;changed['agreements'][5]['historical_price_wan']*=200.
        d,c=train_price_optuna_v4(changed,self.path/'outer_b',overrides=opts,progress=lambda s:None)
        self.assertEqual(a['member_ledger'],c['member_ledger']);self.assertEqual(a['development_metrics'],c['development_metrics'])
        self.assertEqual(predict(b,data['agreements'][4]['params'])['predicted_price_wan'],predict(d,data['agreements'][4]['params'])['predicted_price_wan'])
        self.assertFalse(set(a['fit_record_ids'])&set(a['holdout_record_ids']));self.assertNotEqual(a['holdout_metrics']['mape_percent'],c['holdout_metrics']['mape_percent'])
    def test_strict_quality_blocks_publish_but_keeps_diagnostics(self):
        destination=self.path/'strict'
        with self.assertRaisesRegex(ValueError,'旧运行模型保留'):
            train_price_optuna_v4(fixture(4,40)[-1],destination,overrides={'search':{'n_trials':2},'targets':{'mape_percent_max':.0000001,'r2_min':1.},'v4':{'search':{'mode':'global'},'review':{'max_candidates':1},'publish':{'policy':'require_targets'}}},progress=lambda s:None)
        report=json.loads((destination/'training_report.json').read_text(encoding='utf-8'));self.assertTrue(report['quality_gate_rejected']);self.assertEqual(report['quality_status'],'unqualified_trial_draft')
    def test_invalid_config_is_rejected(self):
        for options in ({'v4':{'search':{'model_seeds':'wrong'}}},{'v4':{'train_gate':{'mode':'bogus'}}},{'input':{'min_samples':3}}):
            with self.assertRaises(ValueError):load_config(overrides=options)


class ModeRegistryTests(unittest.TestCase):
    def test_switches_require_matching_saved_mode_and_never_train(self):
        from tools.product_modes import register,select,selected_pointer,mode_key,descriptor
        from tools.product_price_automl import write_json
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);cfg=load_config();write_json(root/'config/portable.json',{'price_engine':'optuna_v4'});write_json(root/'config/price_optuna_v4.json',cfg)
            directory=root/'outputs/demo';directory.mkdir(parents=True);write_json(directory/'price/effective_config.json',cfg)
            manifest={'status':'ready','price_engine':'optuna_v4','price_model':'a','effectiveness_model':'b','business_package':'c','artifact_sha256':{}}
            for key in ('price_model','effectiveness_model','business_package'):
                (directory/manifest[key]).write_bytes(key.encode('ascii'));manifest['artifact_sha256'][key]=hashlib.sha256(key.encode('ascii')).hexdigest()
            write_json(directory/'prototype_manifest.json',manifest);entry=register(root,{'output':'outputs/demo'})
            self.assertEqual(selected_pointer(root)['output'],'outputs/demo');select(root,strategy='global');self.assertIsNone(selected_pointer(root))
            select(root,strategy='hybrid');self.assertEqual(selected_pointer(root)['mode_key'],entry['mode_key'])
            select(root,experts='off');self.assertIsNone(selected_pointer(root));select(root,experts='on');self.assertIsNotNone(selected_pointer(root))
            select(root,trials=5000);self.assertIsNotNone(selected_pointer(root))

if __name__=='__main__':unittest.main()
