"""Portal route adapter. No management hooks or legacy Flask imports."""
import json
import uuid
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import DeepPriceAnalysis
from .errors import DeepPriceError
from .features import AnalysisFeatures

PREFIX = '/api/deep-price/v1'


def handle_http(handler, method):
    parsed = urlparse(handler.path)
    if parsed.path != PREFIX and not parsed.path.startswith(PREFIX + '/'):
        return False
    request_id = 'DEEP-' + uuid.uuid4().hex
    try:
        raw = None
        if method == 'POST':
            try:
                length = int(handler.headers.get('Content-Length', '0'))
            except ValueError:
                raise DeepPriceError('invalid_input', '请求长度无效')
            if not 0 < length <= 1024 * 1024 or handler.headers.get('Transfer-Encoding'):
                raise DeepPriceError('invalid_input', '请求体必须为1字节至1MB的定长JSON')
            # Consume bounded bodies before rejecting auth/origin. Closing with
            # unread request data can reset the connection on Windows/Python3.8
            # and hide the JSON error response from the client.
            old_timeout = handler.connection.gettimeout()
            try:
                handler.connection.settimeout(5)
                raw = handler.rfile.read(length)
            except OSError:
                raise DeepPriceError('timeout', '请求体读取超时', 504)
            finally:
                handler.connection.settimeout(old_timeout)
        if not handler._is_authenticated():
            raise DeepPriceError('authentication_required', '请先登录后访问深度价格分析', 401)
        # JSON plus exact-origin checks cover browser requests even when Portal
        # authentication is explicitly disabled for a local deployment.
        if handler.headers.get('Sec-Fetch-Site') == 'cross-site':
            raise DeepPriceError('forbidden', '不允许跨站访问深度分析', 403)
        origin = handler.headers.get('Origin')
        if origin:
            source = urlparse(origin)
            scheme = 'https' if handler._request_is_https() else 'http'
            if source.scheme != scheme or source.netloc.lower() != handler.headers.get('Host', '').lower() or source.path not in ('', '/'):
                raise DeepPriceError('forbidden', '请求来源与Portal不一致', 403)
        payload = None
        if method == 'POST':
            if handler.headers.get('Content-Type', '').split(';')[0].strip().lower() != 'application/json':
                raise DeepPriceError('invalid_input', '仅接受application/json')
            try:
                def reject_constant(value):
                    raise ValueError('non-finite constant')
                payload = json.loads(raw.decode('utf-8'), parse_constant=reject_constant)
            except (ValueError, UnicodeError):
                raise DeepPriceError('invalid_input', 'JSON无效或包含非有限数值')
        service = DeepPriceAnalysis(handler.app.store, handler.app.model_gateway)
        status, body = AnalysisFeatures(service).handle(method, unquote(parsed.path[len(PREFIX):]), payload, parse_qs(parsed.query))
        handler._json(body, status)
    except DeepPriceError as exc:
        handler._json(exc.response(request_id), exc.status)
    except Exception:
        # Avoid leaking internal paths, database details or model stack traces.
        handler._json(DeepPriceError('internal_error', '深度分析内部错误', 500).response(request_id), 500)
    return True
