"""P1-2.1: real HTTP wide models, cross-batch guards and scoped rules."""
import copy
from contextlib import closing
import json
from pathlib import Path
import pickle
import sqlite3
import sys
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
import deep_price_comprehensive_test as fixtures
from app.deep_price.analysis import DeepPriceAnalysis, Deadline
from app.deep_price.features import sample_values


class WeightedFixture:
    def predict(self,rows):
        return [sum((i+1)*float(value) for i,value in enumerate(row)) for row in rows]


class ReviewTests(unittest.TestCase):
    setUp=fixtures.ComprehensiveTests.setUp
    tearDown=fixtures.ComprehensiveTests.tearDown
    call=fixtures.ComprehensiveTests.call
    request=fixtures.ComprehensiveTests.request
    get_schema=fixtures.ComprehensiveTests.get_schema
    predict=fixtures.ComprehensiveTests.predict

    def test_decimal_sampling_never_overshoots_business_bounds(self):
        import random
        rng=random.Random(20261031)
        for _ in range(500):
            lo=rng.uniform(-100,100);hi=lo+rng.uniform(.001,50);base=(lo+hi)/2
            field={'type':'number','minimum':lo,'maximum':hi,'allowed_values':[]}
            values=sample_values(field,base,21)
            self.assertTrue(all(lo<=v<=hi for v in values))

    def test_business_integer_search_contract_is_preserved(self):
        with closing(sqlite3.connect(str(self.db))) as conn,conn:
            conn.execute('ALTER TABLE parameter_definitions ADD COLUMN search_type TEXT')
            conn.execute("UPDATE parameter_definitions SET search_type='integer' WHERE parameter_id='size'")
        self.assertEqual(next(f for f in self.get_schema()['fields'] if f['field_id']=='size')['type'],'integer')
        req=self.request();req['parameters']['size']=5.5
        self.assertEqual(self.call('/predict',req)[0],400)
        req=self.request();req['field_id']='size'
        status,result=self.call('/sensitivity/single',req)
        self.assertEqual(status,200,result)
        self.assertTrue(all(float(p['value']).is_integer() for p in result['series'][0]['points']))

    def wide_model(self,count):
        fields=['f%d'%i for i in range(count)]
        bundle={'format_version':'price-native-bundle-1.0','product_code':'WIDE-FIXTURE',
                'model_version':'wide-%d'%count,'virtual_fixture':True,'feature_order':fields,
                'feature_schema':[{'field_name':key,'dtype':'number','required':True,'missing_policy':'reject'} for key in fields],
                'models':{'weighted':WeightedFixture()},'ensemble':{'members':[{'name':'weighted','weight':1}]},
                'target_transform':'identity','model_output_transform':'identity','target_divisor_to_wan':1}
        self.model_path.write_bytes(pickle.dumps(bundle,protocol=4))
        self.price._load()
        parameters={key:10 for key in fields}
        with closing(sqlite3.connect(str(self.db))) as conn,conn:
            conn.execute('DELETE FROM parameter_definitions')
            conn.executemany('INSERT INTO parameter_definitions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                             [(key,key,'','number',1,1,i,'性能','[]','{}',0,20) for i,key in enumerate(fields)])
            conn.execute('UPDATE agreements SET params_json=?',(json.dumps(parameters),))
        request=self.request()
        request['parameters']=parameters
        request['field_ids']=None
        return request

    def test_50_and_60_fields_complete_all_samples_in_bounded_http_batches(self):
        for count in (50,60):
            with self.subTest(fields=count):
                request=self.wide_model(count)
                sizes=[]
                original=self.gateway.predict_price_batch
                def record(items,*args,**kwargs):
                    sizes.append(len(items))
                    return original(items,*args,**kwargs)
                with patch.object(self.gateway,'predict_price_batch',record):
                    status,result=self.call('/sensitivity/top10',request)
                self.assertEqual(status,200,result)
                self.assertEqual(result['compared_field_count'],count)
                self.assertEqual(result['sample_count'],count*9)
                self.assertGreater(len(sizes),1)
                self.assertEqual(result['batch_count'],len(sizes))
                self.assertTrue(all(1<n<=100 for n in sizes))
                self.assertEqual([s['field_id'] for s in result['series']],['f%d'%i for i in range(count-1,count-11,-1)])
                self.assertAlmostEqual(result['base_price_wan'],10*count*(count+1))

    def test_model_replacement_in_later_batch_rejects_entire_analysis(self):
        request=self.wide_model(60)
        original=self.gateway.predict_price_batch
        calls=[]
        def changed(*args,**kwargs):
            calls.append(1)
            if len(calls)==2:self.price.model_instance_id='replacement'
            return original(*args,**kwargs)
        with patch.object(self.gateway,'predict_price_batch',changed):
            status,result=self.call('/sensitivity/top10',request)
        self.assertEqual(status,409,result)
        self.assertNotIn('series',result)
        self.assertNotIn('result_id',result)
        self.assertEqual(len(calls),2)

    def test_business_change_and_restore_between_batches_is_not_missed(self):
        request=self.wide_model(60)
        original=DeepPriceAnalysis.handle
        batches=[]
        def wrapped(core,method,path,*args,**kwargs):
            status,result=original(core,method,path,*args,**kwargs)
            if path=='/predict/batch' and status==200:
                batches.append(1)
                if len(batches)==1:
                    for value in ('changed','unchanged'):
                        with closing(sqlite3.connect(str(self.db))) as conn,conn:
                            conn.execute('UPDATE model_registry SET model_version=?',(value,))
            return status,result
        with patch.object(DeepPriceAnalysis,'handle',wrapped):
            status,result=self.call('/sensitivity/top10',request)
        self.assertEqual(status,409,result)
        self.assertNotIn('series',result)
        self.assertEqual(len(batches),1)

    def test_timeout_budget_is_shared_across_chunks(self):
        request=self.wide_model(60)
        original=DeepPriceAnalysis.handle
        def slow(core,method,path,*args,**kwargs):
            result=original(core,method,path,*args,**kwargs)
            if path=='/predict/batch' and result[0]==200:time.sleep(.25)
            return result
        with patch('app.deep_price.features.Deadline',lambda _:Deadline(.2)),patch.object(DeepPriceAnalysis,'handle',slow):
            status,result=self.call('/sensitivity/top10',request)
        self.assertEqual(status,504,result)
        self.assertNotIn('series',result)

    def rule(self,result):
        return {'confirmed':True,'version':'SCOPED-TEST','source':'synthetic fixture','approved_by':'fixture','approved_at':'2026-10-30',
                'price_unit':'万元','time_direction':'base_to_target','base_year':2025,'ppi':{'2025':100,'2026':110},
                'reference_quantity':10,'quantity_elasticity':-.1,
                'applicability':{k:result[k] for k in ('product_code','model_version','price_model_sha256','price_output_signature')},
                'price_basis':{'kind':'model_unit_price','base_year':2025,'reference_quantity':10,'description':'Synthetic unit-price basis'}}

    def save_rule(self,rule):
        (self.root/'config').mkdir(exist_ok=True)
        (self.root/'config/deep_price_adjustment.json').write_text(json.dumps(rule),encoding='utf-8')

    def test_product_model_hash_and_normalization_scope_must_match(self):
        result=self.predict();rule=self.rule(result)
        payload={'result_id':result['result_id'],'year':2026,'quantity':20,'rule_version':rule['version']}
        self.save_rule(rule)
        self.assertEqual(self.call('/adjustment/status')[1]['status'],'available')
        self.assertEqual(self.call('/price-adjust',payload)[0],200)
        for key in rule['applicability']:
            wrong=copy.deepcopy(rule)
            wrong['applicability'][key]='0'*64 if key.endswith('sha256') or key=='price_output_signature' else 'OTHER'
            self.save_rule(wrong)
            self.assertEqual(self.call('/adjustment/status')[1]['status'],'not_configured',key)
            self.assertEqual(self.call('/price-adjust',payload)[0],409,key)
        self.save_rule(rule)
        self.assertEqual(self.call('/price-adjust',dict(payload,rule_sha256='0'*64))[0],409)

    def test_legacy_unscoped_or_inconsistent_price_basis_rule_fails_closed(self):
        result=self.predict();rule=self.rule(result)
        for key in ('applicability','price_basis'):
            invalid=copy.deepcopy(rule);invalid.pop(key);self.save_rule(invalid)
            self.assertEqual(self.call('/adjustment/status')[0],503)
        rule['price_basis']['base_year']=2024;self.save_rule(rule)
        self.assertEqual(self.call('/adjustment/status')[0],503)


if __name__=='__main__':unittest.main(verbosity=2)
