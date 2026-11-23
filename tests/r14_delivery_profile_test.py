# -*- coding: utf-8 -*-
import base64
import copy
import json
import re
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

from product.delivery_profile import (
    adjust_page,
    data_token,
    model_roles,
    normalize,
    request_allowed,
    required_release_keys,
)

ROOT = Path(__file__).resolve().parents[1]


def _unused_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _ds_release(product_name, base_price):
    model = json.loads((ROOT / 'examples/ds/DEMO_model.json').read_text(encoding='utf-8'))
    model = copy.deepcopy(model)
    model['base'] = float(base_price)
    fields = [
        {'field_name': 'component_type', 'field_label': '部件型号', 'ds_name': '部件型号', 'dtype': 'enum', 'unit': '', 'required': True, 'missing_policy': 'reject', 'allowed_values': ['型号1', '型号2']},
        {'field_name': 'component_count', 'field_label': '部件数量', 'ds_name': '部件数量', 'dtype': 'integer', 'unit': '个', 'required': True, 'missing_policy': 'reject', 'generation_min': 1, 'generation_max': 10},
        {'field_name': 'alarm', 'field_label': '报警功能', 'ds_name': '报警功能', 'dtype': 'boolean', 'unit': '', 'required': True, 'missing_policy': 'reject'},
        {'field_name': 'interface_count', 'field_label': '接口数量', 'ds_name': '接口数量', 'dtype': 'number', 'unit': '个', 'required': True, 'missing_policy': 'reject', 'generation_min': 0, 'generation_max': 20},
        {'field_name': 'accuracy', 'field_label': '精度等级', 'ds_name': '精度等级', 'dtype': 'enum', 'unit': '', 'required': True, 'missing_policy': 'reject', 'allowed_values': ['一级', '二级']},
        {'field_name': 'communication', 'field_label': '通信类型', 'ds_name': '通信类型', 'dtype': 'enum', 'unit': '', 'required': True, 'missing_policy': 'reject', 'allowed_values': ['标准', '增强']},
        {'field_name': 'enhanced', 'field_label': '增强采集功能', 'ds_name': '增强采集功能', 'dtype': 'boolean', 'unit': '', 'required': True, 'missing_policy': 'reject'},
    ]
    ref = {
        'component_type': '型号1',
        'component_count': 1,
        'alarm': False,
        'interface_count': 0,
        'accuracy': '一级',
        'communication': '标准',
        'enhanced': False,
    }
    return {
        'format': 'pipeline-ds-release-1.0',
        'product_code': 'PRICE_ONLY_TEST',
        'product_name': product_name,
        'model': model,
        'fields': fields,
        'reference_parameters': ref,
        'virtual_data': True,
    }


def _http(url, method='GET', payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    headers = {'Accept': '*/*'}
    if data is not None:
        headers['Content-Type'] = 'application/json'
    req = Request(url, data=data, headers=headers, method=method)
    opener = build_opener(ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as resp:
            return resp.status, resp.read()
    except HTTPError as exc:
        return exc.code, exc.read()


class _LoopbackServer:
    def __init__(self, handler):
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = 'http://127.0.0.1:%d' % self.server.server_address[1]

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)


class DeliveryProfileTest(unittest.TestCase):
    def test_full_keeps_historical_data_token_and_pages(self):
        self.assertEqual(data_token('CLIENT_PRODUCT', 'full'), 'CLIENT_PRODUCT')
        self.assertNotEqual(data_token('CLIENT_PRODUCT', 'price_only'), data_token('CLIENT_PRODUCT', 'full'))
        self.assertNotEqual(data_token('CLIENT_PRODUCT', 'price_only'), data_token('CLIENT_PRODUCT', 'recommendation'))
        self.assertTrue(request_allowed('full', '/effectiveness'))
        self.assertTrue(request_allowed('full', '/api/evaluate'))
        self.assertEqual(required_release_keys({}), ('price_model', 'effectiveness_model', 'business_package', 'evaluation_metadata'))

    def test_price_package_blocks_other_products_and_skips_effectiveness_file(self):
        self.assertEqual(normalize('2'), 'price_only')
        self.assertTrue(request_allowed('price_only', '/price'))
        self.assertTrue(request_allowed('price_only', '/model-library'))
        self.assertTrue(request_allowed('price_only', '/api/price-workbench/predict'))
        self.assertFalse(request_allowed('price_only', '/'))
        self.assertFalse(request_allowed('price_only', '/effectiveness'))
        self.assertFalse(request_allowed('price_only', '/recommendation'))
        self.assertFalse(request_allowed('price_only', '/price/deep'))
        self.assertFalse(request_allowed('price_only', '/api/evaluate'))
        self.assertFalse(request_allowed('price_only', '/api/recommend'))
        self.assertFalse(request_allowed('price_only', '/api/generation/request'))
        self.assertFalse(request_allowed('price_only', '/api/effectiveness-workbench/evaluate'))
        self.assertFalse(request_allowed('price_only', '/api/deep-price/v1/predict'))
        self.assertTrue(request_allowed('price_only', '/api/price-workbench/predict'))
        self.assertTrue(request_allowed('price_only', '/login.js'))
        self.assertTrue(request_allowed('price_only', '/admin.js'))
        self.assertTrue(request_allowed('price_only', '/api/admin/snapshot'))
        self.assertTrue(request_allowed('price_only', '/favicon.png'))
        keys = required_release_keys({'delivery_profile': {'id': 'price_only'}})
        self.assertNotIn('effectiveness_model', keys)
        self.assertIn('price_model', keys)

    def test_recommendation_package_keeps_calculation_and_hides_other_pages(self):
        self.assertEqual(normalize('3'), 'recommendation')
        self.assertTrue(request_allowed('recommendation', '/'))
        self.assertTrue(request_allowed('recommendation', '/api/evaluate'))
        self.assertFalse(request_allowed('recommendation', '/price'))
        self.assertFalse(request_allowed('recommendation', '/price/deep'))
        self.assertFalse(request_allowed('recommendation', '/effectiveness'))
        self.assertFalse(request_allowed('recommendation', '/portal'))
        self.assertFalse(request_allowed('recommendation', '/api/price-workbench/schema'))
        self.assertFalse(request_allowed('recommendation', '/api/effectiveness-workbench/evaluate'))
        self.assertFalse(request_allowed('recommendation', '/api/deep-price/v1/schema'))
        self.assertTrue(request_allowed('recommendation', '/api/recommend'))
        self.assertTrue(request_allowed('recommendation', '/api/generation/request'))
        self.assertTrue(request_allowed('recommendation', '/api/bootstrap'))
        self.assertTrue(request_allowed('recommendation', '/app.js'))
        self.assertTrue(request_allowed('recommendation', '/admin.js'))
        self.assertFalse(request_allowed('recommendation', '/api/price-workbench/predict'))
        keys = required_release_keys({'delivery_profile': {'id': 'recommendation'}})
        self.assertIn('effectiveness_model', keys)
        self.assertIn('business_package', keys)

    def test_binary_assets_are_not_decoded(self):
        from product.delivery_profile import adjust_page
        png = b'\x89PNG\r\n\x1a\n' + bytes(range(32))
        self.assertEqual(adjust_page('price_only', 'favicon.png', png), png)
        page = adjust_page('price_only', 'price.html', '<a class="button secondary" href="/portal" target="_top">系统导航</a><a class="button secondary" href="/effectiveness">简易效能评价</a><a class="button secondary" href="/recommendation">智能推荐</a>'.encode('utf-8'))
        self.assertIn('模型库'.encode('utf-8'), page)
        self.assertNotIn(b'/effectiveness', page)

    def test_other_profiles_ignore_legacy_user_database(self):
        import json
        import os
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from product.common import data_directory
        root = Path(tempfile.mkdtemp())
        local = root / 'local'
        legacy = local / 'PipelinePer' / 'data' / 'protocol_demo.db'
        legacy.parent.mkdir(parents=True)
        legacy.write_bytes(b'not a database')
        package = root / 'pkg'
        package.mkdir()
        previous = os.environ.get('LOCALAPPDATA')
        os.environ['LOCALAPPDATA'] = str(local)
        os.environ.pop('PIPELINE_PER_DATA_DIR', None)
        try:
            for profile in ('price_only', 'recommendation'):
                (package / 'PACKAGE_MANIFEST.json').write_text(json.dumps({
                    'release': {'product_code': 'CLIENT_FILE'},
                    'delivery_profile': {'id': profile},
                }), encoding='utf-8')
                with patch('product.compatibility.inspect_database', side_effect=AssertionError('legacy inspected')):
                    target = data_directory(package)
                self.assertNotEqual(target.resolve(), (local / 'PipelinePer').resolve())
                self.assertIn('products', target.parts)
            (package / 'PACKAGE_MANIFEST.json').write_text(json.dumps({
                'release': {'product_code': 'CLIENT_FILE'},
                'delivery_profile': {'id': 'full'},
            }), encoding='utf-8')
            with patch('product.compatibility.inspect_database', return_value={'product_code': 'CLIENT_FILE'}):
                reused = data_directory(package)
            self.assertEqual(reused.resolve(), (local / 'PipelinePer').resolve())
        finally:
            if previous is None:
                os.environ.pop('LOCALAPPDATA', None)
            else:
                os.environ['LOCALAPPDATA'] = previous

    def test_price_package_does_not_watch_effectiveness(self):
        import inspect
        from product.supervisor import Supervisor
        ghost = type('Box', (), {'manifest': {'delivery_profile': {'id': 'price_only'}}})()
        roles = Supervisor.active_roles(ghost)
        self.assertEqual(roles, ('price', 'web'))
        self.assertNotIn('effectiveness', roles)
        source = inspect.getsource(Supervisor.run)
        self.assertIn('for role in self.active_roles()', source)
        self.assertNotIn('for role in ROLES', source)

    def test_price_assemble_skips_effectiveness_generator(self):
        import inspect
        from tools.package_existing_model import assemble
        source = inspect.getsource(assemble)
        self.assertIn("if profile != 'price_only':", source)
        self.assertLess(source.index("if profile != 'price_only':"), source.index('build_effectiveness'))
        self.assertIn("if profile != 'price_only':", source[source.index('build_effectiveness'):])

    def test_navigation_links_on_real_pages_match_profile(self):
        static_dir = ROOT / 'app' / 'static'
        href_re = re.compile(r'<a\b[^>]*\bhref="(/[^"]*)"')
        pages_by_profile = {
            'price_only': ('price.html', 'model-library.html', 'admin.html', 'login.html'),
            'recommendation': ('index.html', 'model-library.html', 'admin.html', 'login.html'),
            'full': ('price.html', 'index.html', 'model-library.html', 'admin.html', 'login.html'),
        }
        for profile, pages in pages_by_profile.items():
            for rel in pages:
                rendered = adjust_page(profile, rel, (static_dir / rel).read_bytes()).decode('utf-8')
                links = href_re.findall(rendered)
                for link in links:
                    self.assertTrue(
                        request_allowed(profile, link),
                        'profile=%s page=%s has disallowed link %s' % (profile, rel, link),
                    )
        # Specific navigation & UI checks for price_only
        price_lib = adjust_page('price_only', 'model-library.html', (static_dir / 'model-library.html').read_bytes()).decode('utf-8')
        self.assertIn('href="/price"', price_lib)
        self.assertNotIn('href="/portal"', price_lib)
        self.assertNotIn('value="effectiveness"', price_lib)
        price_admin = adjust_page('price_only', 'admin.html', (static_dir / 'admin.html').read_bytes()).decode('utf-8')
        self.assertIn('href="/price"', price_admin)
        self.assertIn('href="/model-library"', price_admin)
        self.assertNotIn('href="/portal"', price_admin)
        self.assertNotIn('href="/"', price_admin)
        # Specific navigation checks for recommendation
        rec_lib = adjust_page('recommendation', 'model-library.html', (static_dir / 'model-library.html').read_bytes()).decode('utf-8')
        self.assertIn('href="/"', rec_lib)
        self.assertNotIn('href="/portal"', rec_lib)
        self.assertIn('value="effectiveness"', rec_lib)
        rec_admin = adjust_page('recommendation', 'admin.html', (static_dir / 'admin.html').read_bytes()).decode('utf-8')
        self.assertIn('href="/"', rec_admin)
        self.assertNotIn('href="/portal"', rec_admin)
        # Full retains portal links
        full_lib = adjust_page('full', 'model-library.html', (static_dir / 'model-library.html').read_bytes()).decode('utf-8')
        self.assertIn('href="/portal"', full_lib)
        self.assertEqual(model_roles('price_only'), ('price',))
        self.assertEqual(model_roles('recommendation'), ('price', 'effectiveness'))
        self.assertEqual(model_roles('full'), ('price', 'effectiveness'))

    def test_price_only_real_http_import_activate_predict_and_navigation(self):
        from app.model_service_client import DegradedServiceRuntime, ModelServiceGateway
        from app.server import Application, Handler
        from app.store import Store
        from product.common import read_json, write_json
        from product.model_library import ModelLibrary, active_paths
        from services.common.http_service import ServiceApplication, make_handler
        from services.price_service.ds_backend import load_price_service

        temp = tempfile.TemporaryDirectory(prefix='r14_price_only_', dir=str(ROOT / 'outputs'))
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        package_dir = base / 'pkg'
        data_dir = base / 'data_root'
        (package_dir / 'release').mkdir(parents=True)
        (data_dir / 'config').mkdir(parents=True)
        (data_dir / 'data').mkdir(parents=True)

        model_a_path = package_dir / 'release' / 'ds_release.json'
        model_a = _ds_release('仅价格模型A', 25000.0)
        model_b = _ds_release('仅价格模型B', 85000.0)
        write_json(model_a_path, model_a)
        release_meta = {'price_model': 'release/ds_release.json'}

        # Start real HTTP price service with Model A; NO effectiveness service exists.
        price_holder = {'service': load_price_service(model_a_path)}

        class SwappablePriceApp(ServiceApplication):
            service_name = 'price-prediction-service'
            def health(self): return price_holder['service'].health()
            def schema(self): return price_holder['service'].schema()
            def openapi(self): return price_holder['service'].openapi()
            def handle_post(self, path, payload): return price_holder['service'].handle_post(path, payload)

        price_srv = _LoopbackServer(make_handler(SwappablePriceApp()))
        self.addCleanup(price_srv.close)
        dead_effect_url = 'http://127.0.0.1:%d' % _unused_port()

        gateway = ModelServiceGateway(
            None,
            price_url=price_srv.url,
            effectiveness_url=dead_effect_url,
            timeout=2.0,
            fallback=False,
        )
        runtime = DegradedServiceRuntime(gateway, 'PRICE_ONLY_TEST', '仅价格测试成品')
        store = Store(data_dir / 'data' / 'protocol_demo.db', data_dir / 'absent.csv', runtime, data_dir / 'backups')
        for field in model_a['fields']:
            vtype = {'integer': 'integer', 'boolean': 'boolean', 'enum': 'enum'}.get(field['dtype'], 'number')
            store.admin_upsert('parameters', {
                'parameter_id': field['field_name'],
                'label': field['field_label'],
                'unit': field.get('unit', ''),
                'value_type': vtype,
                'allowed_values_json': json.dumps(field.get('allowed_values') or [], ensure_ascii=False),
                'required': 1,
                'enabled': 1,
            })

        app = Application.__new__(Application)
        app.root = data_dir
        app.static_dir = ROOT / 'app' / 'static'
        app.delivery_profile = 'price_only'
        app.demo_read_only = False
        app.disable_admin = False
        app.auth_enabled = False
        app.auth_username = 'tester'
        app.auth_password = 'secret'
        app.auth_ttl_seconds = 3600
        app.auth_secret = b'0' * 32
        app.login_lock = threading.RLock()
        app.login_attempts = {}
        app.model_execution_mode = 'services'
        app.model_config = {
            'execution_mode': 'services',
            'price_service_url': price_srv.url,
            'effectiveness_service_url': dead_effect_url,
            'timeout_seconds': 2.0,
            'local_fallback': False,
        }
        app.portal_config = {'title': 'Test', 'services': {}}
        app.workbench_defaults = {}
        app.local_runtime = None
        app.model_gateway = gateway
        app.runtime = runtime
        app.store = store
        app.model_data_sync_error = 'effectiveness unavailable'
        app.model_data_sync_warnings = []
        app.model_parameter_coverage = {}
        app.sessions = SimpleNamespace(clear_all=lambda: None)
        app.generation_tasks = SimpleNamespace(invalidate_all=lambda: None)
        app.evaluation_lock = threading.RLock()
        app.evaluation_cache = {}
        app.expert_evaluation_cache = {}
        app._invalidate_runtime_caches = lambda: None

        class TestHandler(Handler):
            pass
        TestHandler.app = app
        web_srv = _LoopbackServer(TestHandler)
        self.addCleanup(web_srv.close)

        # 1. Verify live HTTP page navigation has zero 404s and blocked routes return 404
        href_re = re.compile(r'<a\b[^>]*\bhref="(/[^"]*)"')
        for page_path in ('/price', '/model-library', '/admin'):
            status, body = _http(web_srv.url + page_path)
            self.assertEqual(status, 200, page_path)
            html = body.decode('utf-8')
            links = href_re.findall(html)
            self.assertTrue(links, 'expected nav links on ' + page_path)
            for link in links:
                self.assertTrue(request_allowed('price_only', link), (page_path, link))
                link_status, _ = _http(web_srv.url + link)
                self.assertEqual(link_status, 200, 'broken link %s on %s' % (link, page_path))
        for blocked in ('/', '/portal', '/effectiveness', '/recommendation', '/price/deep'):
            status, _ = _http(web_srv.url + blocked)
            self.assertEqual(status, 404, blocked)

        # 2. Initial model library list & Model A prediction
        status, body = _http(web_srv.url + '/api/model-library')
        self.assertEqual(status, 200)
        listing = json.loads(body.decode('utf-8'))
        self.assertEqual(listing['allowed_roles'], ['price'])
        self.assertEqual(listing['active'], {'price': 'default'})
        self.assertNotIn('effectiveness', listing['current_metadata'])
        self.assertEqual(listing['current_metadata']['price']['schema']['product_name'], '仅价格模型A')

        status, body = _http(web_srv.url + '/api/price-workbench/predict', 'POST', {'parameters': model_a['reference_parameters']})
        self.assertEqual(status, 200, body)
        self.assertAlmostEqual(json.loads(body.decode('utf-8'))['prediction']['predicted_price_wan'], 4.5, places=6)

        # 3. Forbid installing or activating effectiveness model in price_only
        eff_payload = base64.b64encode(json.dumps({'capability_model': {}}).encode('utf-8')).decode('ascii')
        status, body = _http(web_srv.url + '/api/model-library/install', 'POST', {
            'role': 'effectiveness', 'name': 'forbidden', 'filename': 'eff.json', 'content': eff_payload,
        })
        self.assertEqual(status, 400, body)
        self.assertFalse((data_dir / 'models' / 'library' / 'catalog.json').exists())

        status, body = _http(web_srv.url + '/api/model-library/activate', 'POST', {
            'role': 'effectiveness', 'id': 'default',
        })
        self.assertEqual(status, 400, body)
        self.assertFalse((data_dir / 'config' / 'active_models.json').exists())

        # 4. Real install (with real --asset-check subprocess) & switch to Model B without effectiveness service
        model_b_bytes = json.dumps(model_b, ensure_ascii=False).encode('utf-8')
        status, body = _http(web_srv.url + '/api/model-library/install', 'POST', {
            'role': 'price',
            'name': 'DS模型B',
            'filename': 'ds_release.json',
            'content': base64.b64encode(model_b_bytes).decode('ascii'),
        })
        self.assertEqual(status, 200, body)
        installed = json.loads(body.decode('utf-8'))
        asset_id = installed['id']
        self.assertEqual(installed['role'], 'price')

        restarted_roles = []

        def fake_restart(self_lib, role):
            restarted_roles.append(role)
            selected = active_paths(package_dir, data_dir, release_meta, role)
            price_holder['service'] = load_price_service(selected['price'])

        with patch.object(ModelLibrary, '_restart', fake_restart), \
             patch.object(gateway, 'schemas', side_effect=AssertionError('must not call dual-service schemas() in price_only')), \
             patch.object(gateway, 'effectiveness_schema', side_effect=AssertionError('must not call effectiveness_schema() in price_only')):
            status, body = _http(web_srv.url + '/api/model-library/activate', 'POST', {
                'role': 'price', 'id': asset_id,
            })
            self.assertEqual(status, 200, body)
            after_switch = json.loads(body.decode('utf-8'))
            self.assertEqual(after_switch['active'], {'price': asset_id})
            self.assertEqual(after_switch['current_metadata']['price']['schema']['product_name'], '仅价格模型B')
            self.assertEqual(restarted_roles, ['price'])

            # Health manifest and actual prediction now reflect Model B
            status, body = _http(web_srv.url + '/api/health')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body.decode('utf-8'))['manifest']['price']['backend'], 'ds_three_layer')

            status, body = _http(web_srv.url + '/api/price-workbench/predict', 'POST', {'parameters': model_a['reference_parameters']})
            self.assertEqual(status, 200, body)
            self.assertAlmostEqual(json.loads(body.decode('utf-8'))['prediction']['predicted_price_wan'], 10.5, places=6)

            # Switch back to default Model A
            status, body = _http(web_srv.url + '/api/model-library/activate', 'POST', {
                'role': 'price', 'id': 'default',
            })
            self.assertEqual(status, 200, body)
            status, body = _http(web_srv.url + '/api/price-workbench/predict', 'POST', {'parameters': model_a['reference_parameters']})
            self.assertEqual(status, 200, body)
            self.assertAlmostEqual(json.loads(body.decode('utf-8'))['prediction']['predicted_price_wan'], 4.5, places=6)

            # 5. Rollback when price_schema fails after restart
            with patch.object(gateway, 'price_schema', side_effect=RuntimeError('price schema unavailable')):
                status, body = _http(web_srv.url + '/api/model-library/activate', 'POST', {
                    'role': 'price', 'id': asset_id,
                })
            self.assertEqual(status, 500, body)
            self.assertEqual(read_json(data_dir / 'config' / 'active_models.json'), {})
            self.assertEqual(price_holder['service'].schema()['product_name'], '仅价格模型A')

    def test_full_and_recommendation_keep_dual_service_activation_and_login_redirect(self):
        from app.model_service_client import DegradedServiceRuntime
        from app.server import Application, Handler
        from app.store import Store
        from product.common import sha, write_json
        from product.model_library import ModelLibrary

        temp = tempfile.TemporaryDirectory(prefix='r14_dual_', dir=str(ROOT / 'outputs'))
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        store = Store(root / 'data/protocol_demo.db', root / 'absent.csv', DegradedServiceRuntime(None, 'P', '产品'))
        store.admin_upsert('parameters', {'parameter_id': 'size', 'label': '尺寸', 'unit': 'mm', 'value_type': 'number', 'required': 1, 'enabled': 1})

        for profile in ('full', 'recommendation'):
            calls = []
            fake_schemas = {
                'price': {'product_code': 'P', 'model_version': 'p1', 'fields': [{'field_name': 'size', 'unit': 'mm'}]},
                'effectiveness': {'product_code': 'P', 'model_version': 'e1', 'fields': [{'field_name': 'size', 'unit': 'mm'}]},
            }
            gateway = SimpleNamespace(
                product_code='P',
                fallback=False,
                schemas=lambda: (calls.append('schemas') or fake_schemas),
                price_schema=lambda: fake_schemas['price'],
                effectiveness_schema=lambda: fake_schemas['effectiveness'],
            )
            app = SimpleNamespace(
                root=root,
                store=store,
                model_gateway=gateway,
                delivery_profile=profile,
                _bind_runtime=lambda rt: calls.append('bind'),
                on_business_data_changed=lambda: calls.append('changed'),
            )
            lib = ModelLibrary(app)
            asset_id = 'a' * 64
            folder = lib.root / asset_id
            folder.mkdir(exist_ok=True)
            write_json(folder / 'model.json', {'version': 'test'})
            meta = {
                'id': asset_id, 'name': '价格模型', 'role': 'price', 'model_file': 'model.json',
                'metadata_file': None, 'files': {'model.json': sha(folder / 'model.json')},
                'schema': {'fields': [{'field_name': 'size', 'unit': 'mm'}]}, 'health': {},
            }
            write_json(folder / 'asset.json', meta)
            write_json(lib.catalog, [meta])
            with patch.object(lib, '_restart'):
                result = lib.activate('price', asset_id)
            self.assertEqual(calls, ['schemas', 'bind', 'changed'])
            self.assertEqual(result['allowed_roles'], ['price', 'effectiveness'])

        # Verify live HTTP page navigation on recommendation profile has zero 404s
        rec_app = Application.__new__(Application)
        rec_app.root = root
        rec_app.static_dir = ROOT / 'app' / 'static'
        rec_app.delivery_profile = 'recommendation'
        rec_app.demo_read_only = False
        rec_app.disable_admin = False
        rec_app.auth_enabled = False
        rec_app.auth_username = 'u'
        rec_app.auth_password = 'p'
        rec_app.auth_ttl_seconds = 3600
        rec_app.auth_secret = b'1' * 32
        rec_app.login_lock = threading.RLock()
        rec_app.login_attempts = {}

        class RecHandler(Handler):
            pass
        RecHandler.app = rec_app
        rec_srv = _LoopbackServer(RecHandler)
        try:
            href_re = re.compile(r'<a\b[^>]*\bhref="(/[^"]*)"')
            for page_path in ('/', '/model-library', '/admin'):
                status, body = _http(rec_srv.url + page_path)
                self.assertEqual(status, 200, page_path)
                for link in href_re.findall(body.decode('utf-8')):
                    self.assertTrue(request_allowed('recommendation', link), (page_path, link))
                    link_status, _ = _http(rec_srv.url + link)
                    self.assertEqual(link_status, 200, 'broken link %s on %s' % (link, page_path))
            for blocked in ('/portal', '/price', '/price/deep', '/effectiveness'):
                status, _ = _http(rec_srv.url + blocked)
                self.assertEqual(status, 404, blocked)
        finally:
            rec_srv.close()

        # Verify profile-aware login redirects avoid 404 /portal in trimmed profiles
        for profile, expected_redirect in (('full', '/portal'), ('price_only', '/price'), ('recommendation', '/')):
            app = Application.__new__(Application)
            app.root = root
            app.static_dir = ROOT / 'app' / 'static'
            app.delivery_profile = profile
            app.demo_read_only = False
            app.disable_admin = False
            app.auth_enabled = True
            app.auth_username = 'u'
            app.auth_password = 'p'
            app.auth_ttl_seconds = 3600
            app.auth_secret = b'1' * 32
            app.login_lock = threading.RLock()
            app.login_attempts = {}

            class AuthHandler(Handler):
                pass
            AuthHandler.app = app
            srv = _LoopbackServer(AuthHandler)
            try:
                status, body = _http(srv.url + '/api/auth/login', 'POST', {'username': 'u', 'password': 'p', 'next': '/portal'})
                self.assertEqual(status, 200, body)
                self.assertEqual(json.loads(body.decode('utf-8'))['redirect'], expected_redirect)
            finally:
                srv.close()


if __name__ == '__main__':
    unittest.main()
