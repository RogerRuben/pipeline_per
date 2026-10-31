import csv
import io
import json
import math
import sys
import time
import unittest
from pathlib import Path
from urllib.request import Request, ProxyHandler, build_opener
from urllib.error import HTTPError
from http.cookies import SimpleCookie

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from deep_price_contract_test import DeepPriceContractTests
from app.deep_price.results import export_result


class ComprehensiveTests(unittest.TestCase):
    setUp=DeepPriceContractTests.setUp
    tearDown=DeepPriceContractTests.tearDown
    request=DeepPriceContractTests.request
    get_schema=DeepPriceContractTests.get_schema

    def call(self,path,payload=None,session=True,auth=True):
        self.app.root=self.root
        headers={'Content-Type':'application/json'}
        if auth:
            headers['Cookie']=self.cookie+('; dp_session='+self.sid if session and getattr(self,'sid',None) else '')
        req=Request(self.portal.url+'/api/deep-price/v1'+path,headers=headers,
                    data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode('utf-8'))
        try:r=build_opener(ProxyHandler({})).open(req,timeout=10)
        except HTTPError as exc:r=exc
        with r:
            if session:
                c=SimpleCookie();c.load(r.headers.get('Set-Cookie',''))
                if 'dp_session' in c:self.sid=c['dp_session'].value
            body=r.read()
            return r.code,json.loads(body.decode('utf-8')) if 'application/json' in r.headers.get('Content-Type','') else body

    def predict(self):
        status,result=self.call('/predict',self.request())
        self.assertEqual(status,200,result)
        self.assertTrue(result['result_id'])
        return result

    def test_sensitivity_top10_scenario_use_same_service_and_legal_values(self):
        before=self.db.read_bytes(),self.model_path.read_bytes()
        req=self.request()
        status,result=self.call('/sensitivity/single',dict(req,field_id='kind'))
        self.assertEqual(status,200,result)
        self.assertEqual([p['price_wan'] for p in result['series'][0]['points']],[50,190])
        self.assertEqual(result['base_price_wan'],50)
        self.assertEqual(result['metric_kind'],'price_change_rate')
        status,top=self.call('/sensitivity/top10',dict(req,field_ids=['size','kind']))
        self.assertEqual(status,200,top)
        self.assertEqual(top['series'][0]['field_id'],'kind')
        self.assertTrue(all(0<=p['value']<=1000 for s in top['series'] if s['field_id']=='size' for p in s['points']))
        req.pop('parameters');req['items']=[{'item_id':'one','name':'方案甲','parameters':{'size':5,'kind':'经济'}},
                                          {'item_id':'two','parameters':{'size':12,'kind':'2'}}]
        status,scenario=self.call('/scenario',req)
        self.assertEqual(status,200,scenario)
        self.assertEqual([v['result']['predicted_price_wan'] for v in scenario['items']],[50,204])
        self.assertEqual(scenario['items'][0]['label'],'方案甲')
        self.assertEqual(before,(self.db.read_bytes(),self.model_path.read_bytes()))
        self.assertEqual(self.call('/sensitivity/single',dict(self.request(),field_id='size',values=[-5]))[0],400)

    def test_zero_base_change_rates_are_null(self):
        req=self.request();req['parameters']={'size':0,'kind':None}
        status,result=self.call('/sensitivity/single',dict(req,field_id='size',values=[0,1,2]))
        self.assertEqual(status,200,result)
        self.assertEqual(result['base_price_wan'],0)
        self.assertTrue(all(p['change_rate_pct'] is None for p in result['series'][0]['points']))

    def test_server_export_is_session_isolated_and_cannot_be_forged(self):
        result=self.predict();payload={'result_id':result['result_id'],'format':'csv'}
        status,data=self.call('/export',payload)
        self.assertEqual(status,200,data)
        text=data.decode('utf-8-sig');self.assertIn(result['price_model_sha256'],text);self.assertIn('50.0',text)
        self.assertEqual(self.call('/export',payload,session=False)[0],404)
        self.assertEqual(self.call('/export',dict(payload,predicted_price_wan=1))[0],400)
        self.assertEqual(self.call('/export',payload,auth=False)[0],401)
        status,data=self.call('/export',dict(payload,format='txt'))
        self.assertEqual(json.loads(data.decode('utf-8'))['predicted_price_wan'],50)
        self.app.deep_price_results.clock=lambda:time.monotonic()+1801
        self.assertEqual(self.call('/export',payload)[0],404)

    def test_csv_formula_injection_is_escaped_but_numeric_prices_preserved(self):
        data,_=export_result({'label':' =HYPERLINK("x")','field':'@SUM(1)','negative_price':-1.5,'safe':'普通文本'},'csv')
        rows=list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        self.assertTrue(rows[1][1].startswith("'"));self.assertTrue(rows[2][1].startswith("'"))
        self.assertEqual(rows[3][1],'-1.5')

    def test_rule_is_unconfigured_until_confirmed_and_adjustment_is_separate(self):
        result=self.predict();payload={'result_id':result['result_id'],'year':2026,'quantity':20,'rule_version':'TEST-ONLY'}
        self.assertEqual(self.call('/adjustment/status')[1]['status'],'not_configured')
        self.assertEqual(self.call('/price-adjust',payload)[0],422)
        rule={'confirmed':True,'version':'TEST-ONLY','source':'synthetic test indices, not a production rule',
              'approved_by':'fixture','approved_at':'2026-10-30','price_unit':'万元','time_direction':'base_to_target',
              'base_year':2025,'ppi':{'2025':100,'2026':110},'reference_quantity':10,'quantity_elasticity':-.1}
        rule['applicability']={key:result[key] for key in ('product_code','model_version','price_model_sha256','price_output_signature')}
        rule['price_basis']={'kind':'model_unit_price','base_year':2025,'reference_quantity':10,'description':'TEST ONLY model unit price basis'}
        (self.root/'config').mkdir(exist_ok=True)
        (self.root/'config/deep_price_adjustment.json').write_text(json.dumps(rule),encoding='utf-8')
        status,adjusted=self.call('/price-adjust',payload)
        self.assertEqual(status,200,adjusted)
        self.assertEqual(adjusted['original_price_wan'],50)
        self.assertAlmostEqual(adjusted['adjusted_price_wan'],55*2**-.1)
        self.assertEqual(self.call('/price-adjust',dict(payload,quantity=0))[0],400)
        self.assertEqual(self.call('/price-adjust',dict(payload,year=2024))[0],400)
        self.assertEqual(self.call('/price-adjust',dict(payload,rule_version='old'))[0],409)
        self.assertEqual(self.call('/price-adjust',dict(payload,result_id=adjusted['result_id']))[0],400)

    def test_model_metrics_are_real_read_only_or_unavailable(self):
        before=self.db.read_bytes(),self.model_path.read_bytes()
        self.assertEqual(self.call('/feature-importance')[1]['status'],'unavailable')
        self.price.bundle['training_metrics']={'mae':.123}
        self.price.bundle['feature_importance']={'size':.75,'kind':.25}
        status,result=self.call('/model-summary');self.assertEqual(status,200,result)
        self.assertEqual(result['data']['metrics']['training_metrics']['mae'],.123)
        self.assertEqual(self.call('/feature-importance')[1]['data']['feature_importance']['size'],.75)
        self.assertEqual(self.call('/explanation/status')[1]['status'],'not_configured')
        self.predict()
        self.assertEqual(before,(self.db.read_bytes(),self.model_path.read_bytes()))

    def test_logout_invalidates_result_session_even_with_old_auth_cookie(self):
        result=self.predict()
        req=Request(self.portal.url+'/api/auth/logout',data=b'{}',headers={'Content-Type':'application/json','Cookie':self.cookie+'; dp_session='+self.sid})
        with build_opener(ProxyHandler({})).open(req,timeout=5) as r:self.assertEqual(r.status,200)
        self.assertEqual(self.call('/export',{'result_id':result['result_id'],'format':'txt'})[0],404)


if __name__=='__main__':unittest.main(verbosity=2)
