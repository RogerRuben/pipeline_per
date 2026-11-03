"""Historical search reuses definitions, never model results or stale rules."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.store import Store


class Runtime:
    def __init__(self):self.batches=[];self.fail_batch=False
    def evaluate_batch_effectiveness_only(self,items,target_protocol=None):
        self.batches.append(target_protocol)
        if self.fail_batch:raise ValueError('one bad row')
        return [self.evaluate_effectiveness_only(i['parameters'],target_protocol,i['historical_price_wan']) for i in items]
    def evaluate_effectiveness_only(self,params,target_protocol=None,historical_price_wan=None):
        if self.fail_batch and params['size']==13:raise ValueError('bad row 13')
        return {'parameters':dict(params),'predicted_price_wan':historical_price_wan,
                'price_interval_wan':[historical_price_wan,historical_price_wan],
                'price_source':'historical','capability_score':params['size']+(100 if target_protocol=='p2' else 0),
                'feasibility_probability':1.,'cost_effectiveness':1.}


class HistoricalBatchMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.db=Path(self.temp.name)/'data.db'
        with closing(sqlite3.connect(str(self.db))) as conn:
            conn.executescript('''
                CREATE TABLE products(product_code TEXT,enabled INTEGER);
                INSERT INTO products VALUES('P',1);
                CREATE TABLE parameter_definitions(parameter_id TEXT,label TEXT,value_type TEXT,enabled INTEGER,model_value_mapping_json TEXT);
                INSERT INTO parameter_definitions VALUES('size','尺寸','number',1,'{}');
                INSERT INTO parameter_definitions VALUES('kind','类型','enum',1,'{"是":1}');
                CREATE TABLE tags(tag_id TEXT,derivation_mode TEXT,enabled INTEGER);
                INSERT INTO tags VALUES('manual','manual',1);
                INSERT INTO tags VALUES('large','rule',1);
                CREATE TABLE tag_rules(rule_id TEXT,tag_id TEXT,parameter_id TEXT,operator TEXT,value1 TEXT,value2 TEXT,rule_group TEXT,enabled INTEGER);
                INSERT INTO tag_rules VALUES('r','large','size','gte','10',NULL,'default',1);
                CREATE TABLE agreements(agreement_id TEXT,product_code TEXT,agreement_source TEXT,historical_price_wan REAL,params_json TEXT,tags_json TEXT,enabled INTEGER);
            ''')
            conn.executemany('INSERT INTO agreements VALUES(?,?,?,?,?,?,1)',[
                ('P%03d'%i,'P','historical',100.+i,json.dumps({'size':i,'kind':'是'}),'["manual"]') for i in range(160)])
            conn.commit()
        self.store=Store.__new__(Store);self.store.db_path=self.db;self.store.read_only=False;self.store.runtime=Runtime()
    def tearDown(self):self.temp.cleanup()
    def test_160_rows_match_independent_legacy_row_evaluation(self):
        with closing(self.store.connect()) as conn:rows=conn.execute('SELECT * FROM agreements ORDER BY agreement_id').fetchall()
        expected=[self.store._agreement_row(row,recalculate=True) for row in rows]
        with patch.object(self.store,'connect',wraps=self.store.connect) as connections:
            actual=self.store.historical_agreements()
        self.assertEqual(actual,expected)
        self.assertLessEqual(connections.call_count,3)
        self.assertIn('manual',actual[0]['tags']);self.assertNotIn('large',actual[0]['tags'])
        self.assertIn('large',actual[20]['tags'])
        self.assertEqual(actual[20]['evaluation']['model_parameters']['kind'],1)
        self.assertEqual(actual[20]['params']['kind'],'是')
        self.assertEqual(actual[20]['predicted_price_wan'],120.)
    def test_protocol_and_models_are_evaluated_on_each_request(self):
        first=self.store.historical_agreements(target_protocol='p1')
        second=self.store.historical_agreements(target_protocol='p2')
        self.assertEqual(self.store.runtime.batches,['p1','p2'])
        self.assertEqual(second[0]['capability_score']-first[0]['capability_score'],100)
    def test_next_request_reads_updated_mapping_and_rules(self):
        first=self.store.historical_agreements()
        with closing(self.store.connect()) as conn:
            conn.execute('UPDATE parameter_definitions SET model_value_mapping_json=? WHERE parameter_id=?',(json.dumps({'是':2}),'kind'))
            conn.execute("UPDATE tag_rules SET value1='20'")
            conn.commit()
        second=self.store.historical_agreements()
        self.assertEqual(second[15]['evaluation']['model_parameters']['kind'],2)
        self.assertIn('large',first[15]['tags']);self.assertNotIn('large',second[15]['tags'])
    def test_failed_batch_preserves_bad_row_and_reuses_metadata_for_good_rows(self):
        self.store.runtime.fail_batch=True
        with patch.object(self.store,'connect',wraps=self.store.connect) as connections:
            result=self.store.historical_agreements()
        self.assertEqual(len(result),160)
        self.assertFalse(result[13]['model_evaluation_available'])
        self.assertTrue(result[14]['model_evaluation_available'])
        self.assertEqual(result[13]['historical_price_wan'],113.)
        self.assertLessEqual(connections.call_count,3)
    def test_degraded_read_does_not_call_models(self):
        result=self.store.historical_agreements(recalculate=False)
        self.assertEqual(len(result),160);self.assertEqual(self.store.runtime.batches,[])


if __name__=='__main__':unittest.main(verbosity=2)
