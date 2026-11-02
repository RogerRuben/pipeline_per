import json
import tempfile
import unittest
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from types import SimpleNamespace
from unittest.mock import patch
from tests.deep_price_comprehensive_test import ComprehensiveTests
from app.deep_price.schema import fields_for,bound_metadata
from app.deep_price.provider import BusinessSnapshot
from tools.bind_price_evaluation_metadata import bind

class R6Tests(unittest.TestCase):
    setUp=ComprehensiveTests.setUp
    tearDown=ComprehensiveTests.tearDown
    request=ComprehensiveTests.request
    get_schema=ComprehensiveTests.get_schema
    call=ComprehensiveTests.call

    def test_readonly_single_model_library(self):
        before=self.db.read_bytes()
        status,result=self.call('/model-library')
        self.assertEqual(status,200,result)
        self.assertEqual(result['installed_count'],1)
        self.assertTrue(result['items'][0]['readonly'])
        self.assertEqual(result['items'][0]['price_model_sha256'],self.get_schema()['price_model_sha256'])
        self.assertEqual(self.call('/model-library',{})[0],404)
        self.assertEqual(before,self.db.read_bytes())

    def test_importance_selection_preserves_published_order_not_perturbation_rank(self):
        import hashlib
        snapshot=BusinessSnapshot(self.app.store)
        try:
            data=json.loads(snapshot.release['data_json'])
            digest=hashlib.sha256(json.dumps(data,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode('utf-8')).hexdigest()
        finally:snapshot.close()
        identity=self.get_schema()
        self.price.bundle['feature_importance']={'kind':'permutation_mae_increase','model_sha256':identity['price_model_sha256'],
            'business_data_sha256':digest,'items':[{'field_id':'size','importance':10},{'field_id':'kind','importance':1}]}
        status,chosen=self.call('/sensitivity/top10',dict(self.request(),ranking_mode='importance'))
        self.assertEqual(status,200,chosen)
        self.assertEqual([r['field_id'] for r in chosen['series']],['size','kind'])
        self.assertNotEqual(len(chosen['series'][0]['points']),len(chosen['series'][1]['points']))
        status,perturbed=self.call('/sensitivity/top10',dict(self.request(),field_ids=['size','kind']))
        self.assertEqual(status,200,perturbed)
        self.assertEqual(perturbed['series'][0]['field_id'],'kind')

    def test_range_protocol_and_importance_not_invented(self):
        req=self.request()
        status,result=self.call('/sensitivity/single',dict(req,field_id='size',range_mode='historical'))
        self.assertEqual(status,200,result)
        self.assertEqual(result['range_mode'],'historical')
        self.assertEqual(self.call('/sensitivity/top10',dict(req,ranking_mode='importance'))[0],422)
        self.assertEqual(self.call('/sensitivity/single',dict(req,field_id='size',range_mode='invalid'))[0],400)

    def test_scenario_deltas_are_server_computed(self):
        req=self.request();req.pop('parameters')
        req['items']=[{'item_id':'baseline','name':'base','parameters':{'size':5,'kind':'经济'}},
                      {'item_id':'next','name':'new','parameters':{'size':12,'kind':'2'}}]
        status,result=self.call('/scenario',req);self.assertEqual(status,200,result)
        self.assertEqual(result['items'][1]['base_price_wan'],50)
        self.assertEqual(result['items'][1]['delta_wan'],154)
        self.assertEqual(result['items'][1]['change_rate_pct'],308)
        self.assertEqual(len(result['items'][1]['changed_fields']),2)

    def test_unbound_importance_is_unavailable(self):
        snapshot=BusinessSnapshot(self.app.store)
        try:
            meta={'feature_importance':{'kind':'permutation_mae_increase','model_sha256':'wrong','business_data_sha256':'wrong'}}
            result=bound_metadata(meta,snapshot,'actual')
            self.assertIsNone(result['feature_importance'])
            self.assertIsNotNone(meta['feature_importance'])
        finally:snapshot.close()

class R6SchemaTests(unittest.TestCase):
    def test_free_text_cannot_be_selected_for_perturbation(self):
        snapshot=SimpleNamespace(definitions=[{'parameter_id':'free_text','label':'说明','value_type':'string'}])
        fields,_=fields_for(snapshot,{'fields':[{'field_name':'free_text','dtype':'string'}]})
        self.assertFalse(fields[0]['analysis_supported'])
        self.assertTrue(fields[0]['analysis_unavailable_reason'])

    def test_metric_units_are_explicit_and_model_sha_bound(self):
        with tempfile.TemporaryDirectory() as directory:
            d=Path(directory);model=d/'m.pkl';model.write_bytes(b'fixture-not-loaded')
            report=d/'report.json';report.write_text(json.dumps({'engine':'optuna_v3','validation_metrics':{'mape':.042}}))
            with patch('tools.release_analysis_metadata.enrich',return_value={}):
                bind(model,report,d/'metadata.json')
            result=json.loads((d/'metadata.json').read_text())
            self.assertEqual(result['metric_units']['validation_metrics']['mape'],'ratio')
            self.assertEqual(result['metrics']['validation_metrics']['mape'],.042)
            self.assertEqual(result['metric_units']['selected_training_metrics']['mape_percent'],'percent')


class OfflineImportanceTests(unittest.TestCase):
    def test_fixed_validation_rows_and_no_model_mutation(self):
        import hashlib
        from tools.release_analysis_metadata import enrich
        with tempfile.TemporaryDirectory() as directory:
            d=Path(directory);model=d/'trusted.pkl';model.write_bytes(b'unchanged')
            agreements=[{'agreement_id':'S'+str(n),'params':{'x':n,'fixed':1},'historical_price_wan':n*2} for n in range(8)]
            business={'product_code':'P','data':{'parameters':[{'parameter_id':'x','label':'x'},{'parameter_id':'fixed','label':'fixed'}],'agreements':agreements}}
            source=d/'business.json';source.write_text(json.dumps(business))
            bundle={'source_feature_order':['x','fixed'],'ensemble':{'members':[{'name':'linear','weight':1}]},'feature_schema':{}}
            report={'validation_record_ids':['S0','S1','S2','S3']}
            with patch('services.price_service.native_bundle.load_bundle',return_value=bundle),patch('services.price_service.native_bundle.predict_batch',side_effect=lambda b,rows:[{'predicted_price_wan':r['x']*2} for r in rows]):
                result=enrich(model,source,report)
                repeated=enrich(model,source,report)
            self.assertEqual(result,repeated)
            value=result['feature_importance']
            self.assertEqual(value['sample_ids'],['S0','S1','S2','S3'])
            self.assertEqual(value['scope'],'selected_search_validation_not_independent_test')
            self.assertGreater(value['items'][0]['importance'],0)
            self.assertEqual(value['items'][1]['importance'],0)
            self.assertEqual(value['items'][0]['ratio'],1)
            self.assertEqual(value['model_sha256'],hashlib.sha256(b'unchanged').hexdigest())
            self.assertEqual(model.read_bytes(),b'unchanged')


class PortIsolationTests(unittest.TestCase):
    def test_live_http_listener_cannot_be_rebound_by_reuse_address(self):
        import os
        from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
        from services.common.http_service import ExclusiveThreadingHTTPServer
        first=ExclusiveThreadingHTTPServer(('127.0.0.1',0),BaseHTTPRequestHandler)
        second=None
        try:
            try:second=ThreadingHTTPServer(first.server_address,BaseHTTPRequestHandler)
            except OSError:pass
            self.assertIsNone(second)
        finally:
            if second:second.server_close()
            first.server_close()

if __name__=='__main__':unittest.main()


