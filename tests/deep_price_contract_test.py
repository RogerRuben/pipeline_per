"""Real loopback HTTP + isolated SQLite tests. No external models or packages."""
import hashlib
import json
import pickle
import shutil
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.deep_price import DeepPriceAnalysis
from app.deep_price.provider import BusinessSnapshot
from app.model_service_client import ModelServiceGateway, _json_request, build_model_request
from app.server import Handler, Application
from app.store import Store
from services.common.http_service import make_handler
from services.price_service.app import PriceService


class LinearFixture:
    def predict(self, rows):
        return [float(row[0]) + float(row[1]) * 10 for row in rows]


def bundle_bytes(version='fixture-v1'):
    return pickle.dumps({'format_version': 'price-native-bundle-1.0', 'product_code': 'MODEL_DIAGNOSTIC_CODE',
        'model_version': version, 'virtual_fixture': True, 'feature_order': ['size', 'kind'],
        'feature_schema': [{'field_name': 'size', 'field_label': '尺寸', 'unit': 'mm', 'dtype': 'number', 'required': True, 'missing_policy': 'reject'},
                           {'field_name': 'kind', 'field_label': '类别', 'unit': '', 'dtype': 'number', 'required': False, 'missing_policy': 'zero'}],
        'models': {'linear': LinearFixture()}, 'ensemble': {'members': [{'name': 'linear', 'weight': 1}]},
        'target_transform': 'identity', 'model_output_transform': 'identity', 'target_divisor_to_wan': 1}, protocol=4)


class RunningServer:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%s' % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class DeepPriceContractTests(unittest.TestCase):
    def setUp(self):
        output = ROOT/'outputs'
        output.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='deep_price_', dir=str(output))
        self.root = Path(self.temp.name)
        self.model_path = self.root/'price.pkl'
        self.model_path.write_bytes(bundle_bytes())
        self.price = PriceService(self.model_path)
        handler = make_handler(self.price)
        handler.log_message = lambda *args: None
        self.price_server = RunningServer(handler)
        self.gateway = ModelServiceGateway(price_url=self.price_server.url, effectiveness_url='http://127.0.0.1:1',
                                           price_output_config={'unit': 'wan_yuan', 'scale': 2})
        self.db = self.root/'business.db'
        with closing(sqlite3.connect(str(self.db))) as conn, conn:
            conn.executescript('''CREATE TABLE products(product_code TEXT,enabled INTEGER);
                CREATE TABLE product_releases(release_id TEXT,product_code TEXT,status TEXT,data_json TEXT,activated_at TEXT);
                CREATE TABLE parameter_definitions(parameter_id TEXT,label TEXT,unit TEXT,value_type TEXT,
                    required INTEGER,enabled INTEGER,display_order INTEGER,parameter_group TEXT,
                    allowed_values_json TEXT,model_value_mapping_json TEXT,min_value REAL,max_value REAL);
                CREATE TABLE agreements(agreement_id TEXT,agreement_name TEXT,product_code TEXT,enabled INTEGER,
                    agreement_source TEXT,params_json TEXT);
                CREATE TABLE model_registry(model_kind TEXT,model_version TEXT);
                INSERT INTO products VALUES('BUSINESS',1);
                INSERT INTO product_releases VALUES('REL-1','BUSINESS','active','{}','2026-10-30');
                INSERT INTO model_registry VALUES('price','unchanged');''')
            conn.execute('INSERT INTO parameter_definitions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         ('size', '尺寸', 'mm', 'number', 1, 1, 1, '性能', '[]', '{}', 0, 1000))
            # Mapping output "2" is also a business key with another value:
            # applying mapping twice changes price from 25 to 95 (before scale).
            conn.execute('INSERT INTO parameter_definitions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                         ('kind', '类别', '', 'enum', 0, 1, 2, '功能', '["经济","2"]', '{"经济":2,"2":9}', None, None))
            conn.executemany('INSERT INTO agreements VALUES(?,?,?,?,?,?)', [
                ('A1','方案甲','BUSINESS',1,'historical','{"size":5,"kind":"经济"}'),
                ('A2','方案乙','BUSINESS',1,'imported','{"size":8}'),
                ('HIDDEN','停用','BUSINESS',0,'historical','{}'),
                ('OTHER','其他产品','OTHER',1,'historical','{}')])
        self.store = Store.__new__(Store)
        self.store.db_path, self.store.read_only = self.db, True
        self.analysis = DeepPriceAnalysis(self.store, self.gateway)
        self.app = Application.__new__(Application)
        self.app.store, self.app.model_gateway = self.store, self.gateway
        self.app.auth_enabled, self.app.auth_username = True, 'tester'
        self.app.auth_secret, self.app.auth_ttl_seconds = b'test-only-secret', 300
        self.app.price_workbench_schema = self.gateway.price_schema
        class TestHandler(Handler):
            app = self.app
            def log_message(self, *args):
                pass
        self.portal = RunningServer(TestHandler)
        self.cookie = 'ipdemo_auth=' + self.app.make_auth_token('tester')

    def tearDown(self):
        self.portal.close()
        self.price_server.close()
        self.temp.cleanup()

    def get_schema(self):
        status, schema = self.analysis.handle('GET', '/schema')
        self.assertEqual(status, 200, schema)
        return schema

    def request(self, batch=False):
        schema = self.get_schema()
        fields = ('product_code','release_id','model_version','price_model_sha256','model_instance_id',
                  'business_snapshot_sha256','price_output_signature')
        result = {key:schema[key] for key in fields}
        result.update({'items': [{'item_id':'one','parameters':{'size':5,'kind':'经济'}},
                                 {'item_id':'two','parameters':{'size':8}}]} if batch else {'parameters':{'size':5,'kind':'经济'}})
        return result

    def http(self, path, payload=None, cookie=True, headers=None):
        h = {'Content-Type':'application/json'}
        if cookie:
            h['Cookie'] = self.cookie
        h.update(headers or {})
        request = Request(self.portal.url+'/api/deep-price/v1'+path,
                          data=None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8'), headers=h)
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=5)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.loads(response.read().decode('utf-8'))

    def test_same_price_workbench_recommendation_wire_single_batch_and_mapping_once(self):
        request = self.request()
        status, result = self.http('/predict', request)
        self.assertEqual(status, 200, result)
        self.assertEqual(result['predicted_price_wan'], 50)
        self.assertEqual(result['business_parameters']['kind'], '经济')
        workbench = self.app.price_workbench_predict({'parameters': request['parameters']})
        self.assertEqual(workbench['prediction']['predicted_price_wan'], result['predicted_price_wan'])
        # Same envelope builder and output normalizer as the recommendation path.
        encoded = self.store.runtime_parameters(self.store.canonical_business_parameters(request['parameters']))
        wire = _json_request(self.price_server.url+'/api/v1/predict', build_model_request('price', encoded, product_code='BUSINESS'))
        self.assertEqual(self.gateway.price_normalizer.normalize_response(wire)['prediction']['predicted_price_wan'], result['predicted_price_wan'])
        status, batch = self.http('/predict/batch', self.request(True))
        self.assertEqual(status, 200, batch)
        self.assertEqual([i['item_id'] for i in batch['items']], ['one','two'])
        self.assertEqual([i['result']['predicted_price_wan'] for i in batch['items']], [50,16])
        (ROOT/'logs').mkdir(exist_ok=True)
        (ROOT/'logs/deep_price_fixture_identity.json').write_text(json.dumps({
            'native_fixture': {'model_version':result['model_version'], 'sha256':result['price_model_sha256'],
                               'price_unit':'万元', 'normalized_price':result['predicted_price_wan']},
            'portable_virtual_sha256':hashlib.sha256((ROOT/'models/price_bundle.json').read_bytes()).hexdigest(),
            'formal_v3':False},ensure_ascii=False,indent=2),encoding='utf-8')

    def test_read_queries_and_predictions_do_not_change_database_or_model(self):
        before = hashlib.sha256(self.db.read_bytes()).hexdigest(), self.model_path.read_bytes()
        status, products = self.http('/products?page=1&page_size=1')
        self.assertEqual(status, 200, products)
        self.assertEqual(products['total'], 2)
        self.assertEqual(products['items'][0]['product_id'], 'A1')
        self.assertEqual(self.http('/products/A1')[1]['item']['parameters']['kind'], '经济')
        self.assertEqual(self.http('/products/OTHER')[0], 404)
        self.assertEqual(self.http('/predict', self.request())[0], 200)
        self.assertEqual(before, (hashlib.sha256(self.db.read_bytes()).hexdigest(), self.model_path.read_bytes()))
        snap = BusinessSnapshot(self.store)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                snap.conn.execute("UPDATE products SET enabled=0")
        finally:
            snap.close()

    def test_no_local_fallback_or_effectiveness_dependency(self):
        self.assertEqual(DeepPriceAnalysis(self.store, None).handle('GET','/schema')[0], 503)
        self.gateway.price_url = 'http://127.0.0.1:1'
        self.assertEqual(self.analysis.handle('GET','/schema')[0], 503)

    def test_authentication_origin_content_type_and_json_errors(self):
        status, error = self.http('/schema', cookie=False)
        self.assertEqual(status, 401)
        self.assertEqual(error['error']['code'], 'authentication_required')
        self.assertEqual(self.http('/predict', {}, headers={'Origin':'https://evil.example'})[0], 403)
        self.assertEqual(self.http('/schema', headers={'Sec-Fetch-Site':'cross-site'})[0], 403)
        self.assertEqual(self.http('/predict', {}, headers={'Content-Type':'text/plain'})[0], 400)
        self.assertEqual(self.http('/predict', [1])[0], 400)
        self.assertEqual(self.http('/upload-model', {})[0], 404)

    def test_invalid_batch_atomic_limits_and_inputs(self):
        request = self.request(True)
        request['items'] *= 51
        self.assertEqual(self.http('/predict/batch', request)[0], 413)
        request = self.request(True)
        request['items'][1]['item_id'] = 'one'
        with patch.object(self.gateway, 'predict_price_batch', wraps=self.gateway.predict_price_batch) as predict:
            self.assertEqual(self.http('/predict/batch', request)[0], 400)
            predict.assert_not_called()
        for parameters in [{'size':3.5,'kind':'未知'}, {'尺寸':5}, {'size':'bad'}, {'size':[1]}, {'size':-5}, {'size':True}]:
            request = self.request()
            request['parameters'] = parameters
            self.assertEqual(self.http('/predict', request)[0], 400, parameters)
        self.assertEqual(self.http('/products?page=0')[0], 400)

    def test_missing_value_is_owned_by_model_and_bad_model_input_is_400(self):
        request = self.request()
        request['parameters'] = {'size':7, 'kind':None}
        self.assertEqual(self.http('/predict', request)[1]['predicted_price_wan'], 14)
        request['parameters'] = {'kind':'经济'}
        status, error = self.http('/predict', request)
        self.assertEqual(status, 400, error)
        self.assertEqual(error['error']['code'], 'invalid_input')

    def test_stale_token_and_model_restart_or_change_rejected(self):
        request = self.request()
        request['release_id'] = 'OLD'
        self.assertEqual(self.http('/predict', request)[0], 409)
        request = self.request()
        self.price.model_instance_id = 'new-instance'
        self.assertEqual(self.http('/predict', request)[0], 409)
        old = self.price.model_sha256
        self.model_path.write_bytes(bundle_bytes('replacement'))
        self.assertEqual(self.price.health()['model_sha256'], old)
        self.assertNotEqual(old, hashlib.sha256(self.model_path.read_bytes()).hexdigest())

    def test_concurrent_business_write_is_rejected_after_http(self):
        request = self.request()
        original = self.gateway.predict_price
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            with closing(sqlite3.connect(str(self.db))) as conn, conn:
                conn.execute("UPDATE model_registry SET model_version='changed'")
            return result
        with patch.object(self.gateway, 'predict_price', changed):
            self.assertEqual(self.http('/predict', request)[0], 409)

    def test_model_change_during_prediction_rejected(self):
        request = self.request()
        original = self.gateway.predict_price
        def changed(*args, **kwargs):
            result = original(*args, **kwargs)
            self.price.model_instance_id = 'restarted'
            return result
        with patch.object(self.gateway, 'predict_price', changed):
            self.assertEqual(self.http('/predict', request)[0], 409)

    def test_response_order_corruption_rejected_and_deadline_shared(self):
        request = self.request(True)
        original = self.gateway.predict_price_batch
        def reversed_result(*args, **kwargs):
            result = original(*args, **kwargs)
            result['items'].reverse()
            return result
        with patch.object(self.gateway, 'predict_price_batch', reversed_result):
            self.assertEqual(self.http('/predict/batch', request)[0], 503)
        self.analysis.budget_seconds = .01
        original = self.gateway.price_health
        def slow(*args, **kwargs):
            time.sleep(.02)
            return original(*args, **kwargs)
        with patch.object(self.gateway, 'price_health', slow):
            self.assertEqual(self.analysis.handle('GET','/schema')[0], 504)

    def test_normalization_change_and_unverifiable_model_fail_closed(self):
        request = self.request()
        self.gateway.set_price_output_config({'unit':'yuan','scale':1})
        self.assertEqual(self.http('/predict', request)[0], 409)
        self.price.model_sha256 = None
        self.assertEqual(self.http('/schema')[0], 503)

    def test_no_active_release_and_missing_original_excel(self):
        self.assertFalse((self.root/'input/products.xlsx').exists())
        self.assertEqual(self.http('/predict',self.request())[0], 200)
        with closing(sqlite3.connect(str(self.db))) as conn, conn:
            conn.execute("UPDATE product_releases SET status='draft'")
        self.assertEqual(self.http('/schema')[0], 503)

    def test_real_application_published_product_and_existing_service(self):
        # Full Application initialization and actual publish flow in a private
        # sandbox, beyond the small schema fixture used by boundary tests.
        root = self.root/'application'
        root.mkdir()
        for name in ('models', 'data_master'):
            shutil.copytree(str(ROOT/name), str(root/name))
        with patch.dict(os.environ, {'IPDEMO_MODEL_EXECUTION_MODE':'local', 'IPDEMO_DEMO_READ_ONLY':'0'}):
            app = Application(root)
        release = app.product_releases.clone_current()
        app.product_releases.activate(release['release_id'])
        price = PriceService(None, ROOT/'models/price_bundle.json')
        handler = make_handler(price)
        handler.log_message = lambda *args: None
        server = RunningServer(handler)
        try:
            app.model_gateway = ModelServiceGateway(price_url=server.url, effectiveness_url='http://127.0.0.1:1')
            analysis = DeepPriceAnalysis(app.store, app.model_gateway)
            status, schema = analysis.handle('GET','/schema')
            self.assertEqual(status, 200, schema)
            status, products = analysis.handle('GET','/products')
            self.assertEqual(status, 200, products)
            self.assertTrue(products['items'])
            parameters = products['items'][0]['parameters']
            # Product rows may contain historical fields no longer enabled;
            # the client exposes only the active schema's editable field set.
            allowed = {f['field_id'] for f in schema['fields']}
            parameters = {k:v for k,v in parameters.items() if k in allowed}
            request = {k:schema[k] for k in ('product_code','release_id','model_version','price_model_sha256',
                       'model_instance_id','business_snapshot_sha256','price_output_signature')}
            request['parameters'] = parameters
            before = app.store.export_json()
            status, result = analysis.handle('POST','/predict',request)
            self.assertEqual(status, 200, result)
            workbench = app.price_workbench_predict({'parameters':parameters})
            self.assertAlmostEqual(result['predicted_price_wan'], workbench['prediction']['predicted_price_wan'])
            self.assertEqual(before, app.store.export_json())
            self.assertFalse((root/'input/products.xlsx').exists())
        finally:
            server.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
