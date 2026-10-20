# -*- coding: utf-8 -*-
import copy
import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
from threadpoolctl import threadpool_limits
from sklearn.base import clone
from app.xlsx_utils import write_workbook_bytes
from tools.product_price_optuna_v2 import load_config,dataset,workbook_config,training_source_rows,train_price_optuna_v2,expert_rating
from services.price_service.optuna_v2 import price_automl_core as core
from services.price_service.optuna_v2.optimizer import _original_baseline,_select_pareto
from services.price_service.optuna_v2.native_member import NativeOptunaMember
from services.price_service.native_bundle import load_bundle,predict,predict_batch
from services.price_service.app import PriceService


def fixture(n=16,p=36):
    rng=np.random.RandomState(617);X=rng.uniform(.1,1.,(n,p));y=np.exp(3.8+.7*X[:,0]+.3*X[:,1]+rng.normal(0,.025,n))
    specs=[{'parameter_id':'x%d'%j,'label':'价格属性%d'%j,'value_type':'number','unit':'','required':True,'inference':{'observed_count':n}} for j in range(p)]
    data={'products':[{'product_code':'OPTUNA_V2_TEST','product_name':'联合搜索流水线验证'}],'parameters':specs,
          'agreements':[{'agreement_id':'O%03d'%i,'product_code':'OPTUNA_V2_TEST','historical_price_wan':float(y[i]),'params':{'x%d'%j:float(X[i,j]) for j in range(p)}} for i in range(n)]}
    return X,y,specs,data


def workbook(data):
    specs=data['parameters'];rows=[['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs],['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs]]
    rows.extend([r['agreement_id'],'成品%d'%i,r['historical_price_wan']]+[r['params'][p['parameter_id']] for p in specs] for i,r in enumerate(data['agreements']))
    return write_workbook_bytes([('成品数据',rows),('价格属性评分',[['英文代号','中文名','价格重要性评分'],['x0','价格属性0',5],['x1','价格属性1',1]])])


class OptunaV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holder=tempfile.TemporaryDirectory();cls.path=Path(cls.holder.name);cls.X,cls.y,cls.specs,cls.data=fixture()
        cls.source=cls.path/'products.xlsx';cls.source.write_bytes(workbook(cls.data))
        with threadpool_limits(limits=1):cls.bundle,cls.report=train_price_optuna_v2(cls.data,cls.path/'trained',overrides={'search':{'n_trials':3}},input_path=cls.source,progress=lambda s:None)
    @classmethod
    def tearDownClass(cls):cls.holder.cleanup()
    def test_supplied_core_has_not_changed(self):
        base=ROOT/'services/price_service/optuna_v2';record=json.loads((base/'SOURCE_PROVENANCE.json').read_text(encoding='utf-8'))
        self.assertEqual(hashlib.sha256((base/'price_automl_core.py').read_bytes()).hexdigest(),record['original_core_sha256'])
    def test_original_expert_tree_is_cloneable(self):
        m=core.ExpertWeightedTreeRegressor(expert_map=None);self.assertIsNone(clone(m).expert_map)
        m=core.ExpertWeightedTreeRegressor(expert_map={});self.assertEqual(clone(m).expert_map,{})
    def test_original_diagnostic_columns_are_excluded_without_dropping_first_feature(self):
        data=copy.deepcopy(self.data)
        for name in ('Cluster','is_outlier'):
            data['parameters'].append(dict(data['parameters'][0],parameter_id=name,label='诊断列'))
            for row in data['agreements']:row['params'][name]=1.
        frame,*_=dataset(data,load_config());self.assertEqual(frame.shape,(16,36));self.assertEqual(frame.columns[0],'x0')
    def test_import_scores_are_price_ratings_and_english_ids(self):
        cfg=workbook_config(self.source,load_config(),{'x%d'%j for j in range(36)})
        self.assertEqual(cfg['expert']['scores'],{'x0':5.,'x1':1.})
        rows=json.loads((self.path/'trained/price_attribute_scores.json').read_text(encoding='utf-8'))['attributes'];values={r['field_id']:r for r in rows}
        self.assertEqual(values['x0']['model_multipliers'],{'lasso':3.,'ridge':5.,'svr':2.5})
        self.assertEqual(values['x1']['model_multipliers'],{'lasso':.5,'ridge':.3,'svr':.5})
        self.assertEqual(values['x2']['score'],1.)
    def test_six_model_parameters_and_real_split_seed_are_preserved(self):
        self.assertEqual(self.report['training_rows'],16);self.assertEqual(self.report['feature_count'],36)
        self.assertEqual(self.report['trials_completed'],3);self.assertEqual(self.report['member_count'],6)
        self.assertEqual(self.report['selected_parameters']['gbdt_n_estimators'],300)
        self.assertEqual(self.report['selected_parameters']['et_min_samples_split'],4)
        self.assertEqual(self.report['validation_test_rows'],4);self.assertEqual(self.report['validation_protocol'],'joint_searched_split')
        frame,y,*_=dataset(self.data,load_config());rows=np.arange(3,19)
        a=core.split_data(frame,y,rows,7527)[-1];b=core.split_data(frame,y,rows,6192)[-1]
        self.assertNotEqual(a.tolist(),b.tolist())
    def test_native_single_batch_and_original_full_model_match(self):
        fitted=core.load_bundle(str(self.path/'trained/price_ensemble_bundle.pkl'))
        import pandas as pd
        expected,_=core.predict_bundle(fitted,pd.DataFrame(self.X,columns=['x%d'%j for j in range(36)]))
        native=load_bundle(self.path/'trained/price_native_bundle.pkl');parameters=[r['params'] for r in self.data['agreements']]
        batch=predict_batch(native,parameters)
        for i in range(16):self.assertAlmostEqual(batch[i]['predicted_price_wan'],expected[i],places=5)
        for i in range(3):self.assertEqual(predict(native,parameters[i]),batch[i])
        self.assertTrue(self.report['full_data_deployment']);self.assertTrue(self.report['selected_split_reload_verified'])
    def test_native_service_explanations_and_chinese_labels(self):
        service=PriceService(self.path/'trained/price_native_bundle.pkl');r=service.handle_post('/api/v1/predict',{'parameters':self.data['agreements'][0]['params']})
        self.assertTrue(r['success']);self.assertEqual(len(r['explanation']['model_contributions']),6)
        self.assertEqual(len(r['explanation']['price_attribute_scores']),36)
        self.assertAlmostEqual(sum(v['weighted_contribution_wan'] for v in r['explanation']['model_contributions']),r['prediction']['predicted_price_wan'],places=5)
        self.assertTrue(all('价格属性' in p['field_label'] for p in service.schema()['fields']))
        self.assertIn('开发划分',r['explanation']['interval_note'])
        for detail in r['explanation']['configuration_details']:
            member=detail['members'][0]
            if member.get('attribute_terms'):self.assertTrue(all(t['space']=='log_price' for t in member['attribute_terms']))
    def test_zero_weight_exclusion_keeps_prediction_identical(self):
        cfg=_original_baseline(7527)
        for name in core.MODEL_NAMES:cfg['weight_'+name]=0.
        cfg['weight_ridge']=1.
        import pandas as pd
        frame=pd.DataFrame(self.X,columns=['x%d'%j for j in range(36)])
        fitted=core.fit_ensemble(frame,self.y,cfg,{'x0':5.});self.assertEqual(list(fitted['models']),['ridge'])
        wrapper=NativeOptunaMember('ridge',fitted['models']['ridge'],list(frame.columns),fitted['expert_map'],{k:k for k in frame.columns},fitted['scaler'])
        expected,_=core.predict_bundle(fitted,frame);actual=wrapper.predict(fitted['scaler'].transform(frame))
        np.testing.assert_allclose(expected,actual,rtol=1e-12)
    def test_physical_rows_and_missing_values_are_explicit(self):
        rows=training_source_rows(self.data,[r['agreement_id'] for r in self.data['agreements']],self.source)
        np.testing.assert_array_equal(rows,np.arange(3,19))
        amended=copy.deepcopy(self.data);amended['agreements'][0]['params'].pop('x0')
        with self.assertRaisesRegex(ValueError,'价格属性0'):dataset(amended,load_config())
    def test_searched_validation_is_not_reported_as_external_test(self):
        self.assertIsNone(self.report['holdout_metrics']['r2']);self.assertFalse(self.report['business_accuracy_certified'])
        self.assertEqual(self.report['cv_metrics']['scope'],'searched_split_development_not_cv')
        for name in ('optuna_trials.csv','pareto_front.csv','selected_split_predictions.csv','selected_split_bundle.pkl','price_native_bundle.pkl.manifest.json'):
            self.assertTrue((self.path/'trained'/name).is_file())
    def test_configuration_keeps_original_defaults_and_rejects_bad_scores(self):
        cfg=load_config();self.assertEqual(cfg['search']['n_trials'],150);self.assertIsNone(cfg['search']['timeout_seconds']);self.assertTrue(cfg['expert']['enabled'])
        for value in (0,6,float('nan'),True):
            with self.assertRaises(ValueError):load_config(overrides={'expert':{'scores':{'x0':value}}})
    def test_supplied_prior_survives_bilingual_renaming_and_explicit_override(self):
        p={'parameter_id':'pressure_bar','label':'压力','unit':'bar'}
        self.assertEqual(expert_rating(p,load_config()),(5.,'supplied_default'))
        self.assertEqual(expert_rating(p,load_config(overrides={'expert':{'scores':{'pressure_bar':2}}})),(2.,'workbook_or_configuration'))
        self.assertEqual(expert_rating(p,load_config(overrides={'expert':{'use_supplied_defaults':False}})),(1.,'default'))
    def test_selected_split_snapshot_is_exportable_as_an_explicit_option(self):
        folder=self.path/'snapshot_mode'
        with threadpool_limits(limits=1):native,r=train_price_optuna_v2(self.data,folder,overrides={'search':{'n_trials':1},'deployment':{'model_scope':'selected_split'}},progress=lambda s:None)
        self.assertFalse(r['full_data_deployment']);self.assertEqual(r['deployment_scope'],'selected_split')
        saved=core.load_bundle(str(folder/'selected_split_bundle.pkl'))
        import pandas as pd
        expected=core.predict_bundle(saved,pd.DataFrame(self.X,columns=['x%d'%j for j in range(36)]))[0][0]
        self.assertAlmostEqual(expected,predict(load_bundle(folder/'price_native_bundle.pkl'),self.data['agreements'][0]['params'])['predicted_price_wan'],places=5)
    def test_installer_preserves_price_scores_targets_and_existing_models(self):
        from tools.upgrade_product_optuna_v2 import upgrade,PATCH_FILES
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);(base/'config').mkdir();(base/'runtime/python38').mkdir(parents=True);(base/'runtime/python38/python.exe').write_bytes(b'placeholder')
            (base/'config/portable.json').write_text(json.dumps({'price_engine':'r4','ports':{'price':1},'product_name':'真实成品'}),encoding='utf-8')
            (base/'config/price_automl_r4.json').write_text(json.dumps({'quality':{'mape_percent_max':7.,'r2_min':.97,'hard_gate':False},'expert':{'scores':{'x0':4},'enabled':False}}),encoding='utf-8')
            for name in PATCH_FILES:
                if name.endswith(('.py','.js','.json')):
                    p=base/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes((ROOT/name).read_bytes())
            state=base/'saved_model.pkl';state.write_bytes(b'original')
            cfg=upgrade(base,'standard');self.assertEqual(cfg['expert']['scores'],{'x0':4});self.assertTrue(cfg['expert']['enabled'])
            self.assertEqual(cfg['targets']['mape_percent_max'],7.);self.assertEqual(cfg['targets']['r2_min'],.97)
            self.assertEqual(state.read_bytes(),b'original')
            portable=json.loads((base/'config/portable.json').read_text(encoding='utf-8'));self.assertEqual(portable['product_name'],'真实成品');self.assertEqual(portable['price_engine'],'optuna_v2')


if __name__=='__main__':unittest.main(verbosity=2)
