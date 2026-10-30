"""Portal route adapter. No management hooks or legacy Flask imports."""
import json
import uuid
import hashlib
import threading
from urllib.parse import parse_qs, unquote, urlparse

from .analysis import DeepPriceAnalysis
from .errors import DeepPriceError
from .features import AnalysisFeatures
from .results import ResultCache, export_result
from .adjustment import adjustment_status, adjust

PREFIX = '/api/deep-price/v1'
STATE_LOCK = threading.Lock()


def cache_for(app):
    with STATE_LOCK:
        if not hasattr(app,'deep_price_results'):
            app.deep_price_results=ResultCache()
        return app.deep_price_results


def invalidate_session(handler):
    cache_for(handler.app).logout(handler._cookie_value('dp_session'))


def send(handler,status,body,sid=None,content_type=None):
    data = body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False,allow_nan=False).encode('utf-8')
    handler.send_response(status)
    handler.send_header('Content-Type',content_type or 'application/json; charset=utf-8')
    handler.send_header('Content-Length',str(len(data)))
    handler.send_header('Cache-Control','no-store')
    if sid:
        handler.send_header('Set-Cookie','dp_session='+sid+'; Path=/; HttpOnly; SameSite=Strict; Max-Age=3600'+('; Secure' if handler._request_is_https() else ''))
    if content_type:
        ext='csv' if content_type.startswith('text/csv') else 'txt'
        handler.send_header('Content-Disposition','attachment; filename="deep-price-result.'+ext+'"')
    handler.end_headers()
    handler.wfile.write(data)


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
        cache=cache_for(handler.app)
        principal=hashlib.sha256(str(handler._cookie_value(handler.AUTH_COOKIE) if handler.app.auth_enabled else 'local').encode('utf-8')).hexdigest()
        sid=cache.session(handler._cookie_value('dp_session'),principal)
        path=unquote(parsed.path[len(PREFIX):])
        service = DeepPriceAnalysis(handler.app.store, handler.app.model_gateway)
        if method=='POST' and path=='/export':
            if not isinstance(payload,dict) or set(payload)!={'result_id','format'}:
                raise DeepPriceError('invalid_input','导出只接受result_id和format，不接受浏览器价格')
            data,ctype=export_result(cache.get(sid,payload['result_id']),payload['format'])
            send(handler,200,data,sid,ctype)
            return True
        if method=='GET' and path=='/adjustment/status':
            status,body=200,adjustment_status(getattr(handler.app,'root','.'))
        elif method=='POST' and path=='/price-adjust':
            status,body=adjust(service,getattr(handler.app,'root','.'),cache,sid,payload)
        else:
            status, body = AnalysisFeatures(service).handle(method,path,payload,parse_qs(parsed.query))
        if method=='POST' and status==200:
            body=cache.put(sid,path,body)
        send(handler,status,body,sid)
    except DeepPriceError as exc:
        send(handler,exc.status,exc.response(request_id))
    except Exception:
        # Avoid leaking internal paths, database details or model stack traces.
        send(handler,500,DeepPriceError('internal_error', '深度分析内部错误', 500).response(request_id))
    return True
