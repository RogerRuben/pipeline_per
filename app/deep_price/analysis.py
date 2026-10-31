import math
import sqlite3
import time
import uuid

from ..model_service_client import ModelServiceUnavailable
from .errors import DeepPriceError
from .provider import BusinessSnapshot
from .schema import encode, fields_for


class Deadline:
    def __init__(self, seconds):
        self.until = time.monotonic() + seconds

    def remaining(self):
        value = self.until - time.monotonic()
        if value <= 0:
            raise DeepPriceError('timeout', '分析请求超过总时间预算', 504)
        return value


class DeepPriceAnalysis:
    def __init__(self, store, gateway, budget_seconds=30):
        self.store, self.gateway, self.budget_seconds = store, gateway, budget_seconds

    def handle(self, method, path, payload=None, query=None):
        request_id = 'DEEP-' + uuid.uuid4().hex
        try:
            return 200, self._handle(method, path, payload, query or {}, request_id)
        except DeepPriceError as exc:
            return exc.status, exc.response(request_id)
        except ModelServiceUnavailable as exc:
            return exc.status, DeepPriceError(exc.code, str(exc), exc.status).response(request_id)
        except (sqlite3.Error, OSError):
            return 503, DeepPriceError('unavailable', '业务发布数据不可读取', 503).response(request_id)
        except (ValueError, TypeError, KeyError, AttributeError):
            return 503, DeepPriceError('unavailable', '业务或模型服务返回数据不符合契约', 503).response(request_id)

    def _handle(self, method, path, payload, query, request_id):
        if self.gateway is None:
            raise DeepPriceError('unavailable', '深度价格分析要求配置HTTP价格服务，禁止本地模型回退', 503)
        known_get = path in ('/schema', '/products') or path.startswith('/products/')
        if not (method == 'GET' and known_get or method == 'POST' and path in ('/predict', '/predict/batch')):
            raise DeepPriceError('not_found', '此阶段未提供该深度分析接口', 404)
        if method == 'POST' and not isinstance(payload, dict):
            raise DeepPriceError('invalid_input', '请求JSON必须为对象')
        deadline = Deadline(self.budget_seconds)
        snapshot = BusinessSnapshot(self.store, deadline.remaining())
        try:
            before = self.gateway.price_health(timeout=deadline.remaining())
            self._valid_health(before)
            schema = self.gateway.price_schema(timeout=deadline.remaining())
            if not isinstance(schema, dict) or not isinstance(schema.get('fields'), list):
                raise DeepPriceError('unavailable', '价格字段schema无效', 503)
            if schema.get('model_version') != before['model_version']:
                raise DeepPriceError('version_conflict', '模型schema与实例不一致', 409)
            fields, specs = fields_for(snapshot, schema)
            normalizer = self.gateway.price_normalizer
            identity = {'product_code': snapshot.product_code, 'release_id': snapshot.release['release_id'],
                        'model_version': before['model_version'], 'price_model_sha256': before['model_sha256'],
                        'price_unit': '万元', 'business_snapshot_sha256': snapshot.fingerprint,
                        'price_output_signature': normalizer.signature(), 'model_instance_id': before['model_instance_id']}
            metadata = dict(identity, request_id=request_id)
            if method == 'POST':
                allowed = set(identity) | ({'parameters'} if path == '/predict' else {'items'})
                if set(payload) - allowed:
                    raise DeepPriceError('invalid_input', '包含未知请求字段')
                for key in identity:
                    if key == 'price_unit':
                        continue
                    if key not in payload:
                        raise DeepPriceError('invalid_input', '缺少版本身份字段：' + key)
                    if payload[key] != identity[key]:
                        raise DeepPriceError('version_conflict', '产品、模型或单位配置已变化，请刷新后重试', 409)
                if 'price_unit' in payload and payload['price_unit'] != '万元':
                    raise DeepPriceError('invalid_input', '价格单位必须为万元')
            if path == '/schema':
                output = dict(metadata, fields=fields, capabilities={'ml': True, 'additive_rules': False,
                              'mixed_adjustment': False, 'explanation': 'not_configured', 'batch_limit': 100})
                output.update(backend=before.get('backend'), fixture=bool(schema.get('virtual_fixture')),
                              published_at=snapshot.release.get('activated_at'), analysis_metadata=schema.get('analysis_metadata') or {})
            elif path == '/products':
                try:
                    page = int((query.get('page') or ['1'])[0])
                    size = int((query.get('page_size') or ['50'])[0])
                    if page < 1 or not 1 <= size <= 100:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise DeepPriceError('invalid_input', '分页参数无效')
                output = dict(metadata, items=snapshot.products[(page-1)*size:page*size], page=page,
                              page_size=size, total=len(snapshot.products))
            elif path.startswith('/products/'):
                item = next((p for p in snapshot.products if p['product_id'] == path[len('/products/'):]), None)
                if item is None:
                    raise DeepPriceError('not_found', '当前发布产品中不存在此记录', 404)
                output = dict(metadata, item=item)
            else:
                items = [{'item_id': request_id, 'parameters': payload.get('parameters')}] if path == '/predict' else payload.get('items')
                if not isinstance(items, list) or not items:
                    raise DeepPriceError('invalid_input', 'items必须是非空数组')
                if len(items) > 100:
                    raise DeepPriceError('batch_too_large', '深度分析单批最多100条', 413)
                ids, prepared, businesses = set(), [], []
                for item in items:
                    if not isinstance(item, dict) or set(item) != {'item_id', 'parameters'}:
                        raise DeepPriceError('invalid_input', '每项仅允许item_id和parameters')
                    item_id = item['item_id']
                    if not isinstance(item_id, str) or not item_id.strip() or len(item_id) > 200 or item_id in ids:
                        raise DeepPriceError('invalid_input', 'item_id须为唯一非空字符串（最多200字符）')
                    ids.add(item_id)
                    business, parameters = encode(snapshot, item['parameters'], fields, specs)
                    prepared.append({'item_id': item_id, 'parameters': parameters})
                    businesses.append(business)
                snapshot.release_read_lock()
                expected = {'expected_model_sha256': before['model_sha256'],
                            'expected_model_instance_id': before['model_instance_id']}
                if path == '/predict':
                    responses = [self.gateway.predict_price(prepared[0]['parameters'], snapshot.product_code,
                                 timeout=deadline.remaining(), request_id=request_id, expected_identity=expected)]
                else:
                    batch = self.gateway.predict_price_batch(prepared, snapshot.product_code, timeout=deadline.remaining(),
                                                             request_id=request_id, expected_identity=expected)
                    self._check_model(batch.get('model'), before)
                    responses = batch.get('items')
                if not isinstance(responses, list) or len(responses) != len(prepared):
                    raise DeepPriceError('unavailable', '价格服务返回条目数量不一致', 503)
                results = []
                for item, business, response in zip(prepared, businesses, responses):
                    self._check_model(response.get('model'), before)
                    price = (response.get('prediction') or {}).get('predicted_price_wan')
                    if response.get('success') is not True or response.get('request_id') != item['item_id'] or isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
                        raise DeepPriceError('unavailable', '价格服务返回无效或乱序结果', 503)
                    results.append({'item_id': item['item_id'], 'result': dict(metadata, success=True, pricing_mode='ml',
                                    predicted_price_wan=price, business_parameters=business)})
                output = results[0]['result'] if path == '/predict' else dict(metadata, success=True, items=results)
            snapshot.assert_unchanged()
            after = self.gateway.price_health(timeout=deadline.remaining())
            self._valid_health(after)
            self._check_model(after, before)
            if normalizer.signature() != self.gateway.price_normalizer.signature():
                raise DeepPriceError('version_conflict', '价格归一化配置已变化', 409)
            snapshot.assert_unchanged()
            deadline.remaining()
            return output
        finally:
            snapshot.close()

    @staticmethod
    def _valid_health(health):
        import re
        if (not isinstance(health, dict) or health.get('status') != 'ok' or not health.get('model_version')
            or not health.get('model_instance_id') or not re.fullmatch('[0-9a-f]{64}', str(health.get('model_sha256', '')))):
            raise DeepPriceError('unavailable', '价格服务未提供可验证的已加载模型身份，请更新价格服务', 503)

    @staticmethod
    def _check_model(model, expected):
        if not isinstance(model, dict) or any(model.get(k) != expected.get(k) for k in ('model_version', 'model_sha256', 'model_instance_id')):
            raise DeepPriceError('version_conflict', '分析期间价格模型实例发生变化', 409)
