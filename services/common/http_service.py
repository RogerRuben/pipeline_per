# -*- coding: utf-8 -*-
"""Dependency-free JSON/HTML HTTP helpers for Windows 7 / Python 3.8."""
from __future__ import print_function

import json
import errno
import mimetypes
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse


class JsonServiceError(Exception):
    def __init__(self, message, status=400, code="bad_request", details=None):
        Exception.__init__(self, message)
        self.status = int(status)
        self.code = str(code)
        self.details = details or {}


class ServiceApplication(object):
    service_name = "model-service"
    service_version = "1.0.0"

    def health(self):
        return {"status": "ok", "service": self.service_name, "service_version": self.service_version}

    def openapi(self):
        raise NotImplementedError

    def docs_html(self):
        spec = self.openapi()
        endpoints = []
        for path, methods in spec.get("paths", {}).items():
            for method, desc in methods.items():
                endpoints.append("<tr><td><code>%s</code></td><td><code>%s</code></td><td>%s</td></tr>" % (
                    method.upper(), path, _html(desc.get("summary", ""))))
        return """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>{title}</title><style>
body{{font-family:'Microsoft YaHei',Arial,sans-serif;margin:0;background:#f4f7fb;color:#1f2937}}header{{background:#17365d;color:white;padding:22px 28px}}main{{max-width:1100px;margin:20px auto;padding:0 18px}}section{{background:white;border-radius:10px;padding:18px;margin-bottom:16px;box-shadow:0 2px 8px rgba(0,0,0,.06)}}code,pre{{font-family:Consolas,monospace}}pre{{background:#0f172a;color:#e2e8f0;padding:14px;overflow:auto;border-radius:8px}}table{{width:100%;border-collapse:collapse}}td,th{{padding:9px;border-bottom:1px solid #e5e7eb;text-align:left}}input,textarea,button{{font:inherit}}textarea{{width:100%;min-height:220px;box-sizing:border-box}}button{{padding:9px 15px;border:0;border-radius:6px;background:#245b8f;color:white;cursor:pointer}}.result{{white-space:pre-wrap;background:#f8fafc;padding:12px;border-radius:8px;min-height:80px}}
</style></head><body><header><h1>{title}</h1><div>版本 {version} · <a href=\"/openapi.json\" style=\"color:#dbeafe\">OpenAPI JSON</a></div></header><main>
<section><h2>服务状态</h2><div id=\"health\">读取中…</div></section>
<section><h2>接口列表</h2><table><thead><tr><th>方法</th><th>路径</th><th>说明</th></tr></thead><tbody>{rows}</tbody></table></section>
<section><h2>在线JSON测试</h2><p>选择接口并粘贴请求JSON。</p><select id=\"endpoint\"><option value=\"{test_path}\">{test_path}</option><option value=\"{batch_path}\">{batch_path}</option></select><textarea id=\"payload\">{example}</textarea><p><button id=\"send\">发送请求</button></p><div class=\"result\" id=\"result\"></div></section>
</main><script>
function pretty(x){{return JSON.stringify(x,null,2)}}
fetch('/health').then(r=>r.json()).then(x=>document.getElementById('health').textContent=pretty(x)).catch(e=>document.getElementById('health').textContent=e.message);
document.getElementById('send').onclick=async function(){{let el=document.getElementById('result');try{{let p=JSON.parse(document.getElementById('payload').value);let r=await fetch(document.getElementById('endpoint').value,{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(p)}});let d=await r.json();el.textContent=pretty(d)}}catch(e){{el.textContent=e.message}}}};
</script></body></html>""".format(
            title=_html(spec.get("info", {}).get("title", self.service_name)),
            version=_html(spec.get("info", {}).get("version", self.service_version)),
            rows="".join(endpoints),
            test_path=_html(self.test_path()), batch_path=_html(self.batch_path()),
            example=_html(json.dumps(self.example_request(), ensure_ascii=False, indent=2)),
        )

    def test_path(self):
        return "/api/v1/evaluate"

    def batch_path(self):
        return self.test_path() + "/batch"

    def example_request(self):
        return {"request_id": "demo", "product_code": "PRODUCT", "parameters": {}}

    def handle_get(self, path):
        if path in ("/", "/docs", "/docs/"):
            return 200, "text/html; charset=utf-8", self.docs_html().encode("utf-8")
        if path == "/health":
            return 200, "application/json; charset=utf-8", _json_bytes(self.health())
        if path == "/openapi.json":
            return 200, "application/json; charset=utf-8", _json_bytes(self.openapi())
        if path == "/api/v1/schema":
            return 200, "application/json; charset=utf-8", _json_bytes(self.schema())
        raise JsonServiceError("接口不存在", 404, "not_found")

    def handle_post(self, path, payload):
        raise JsonServiceError("接口不存在", 404, "not_found")


def _html(value):
    return str(value).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _json_bytes(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def _peer_disconnected(exc):
    return (isinstance(exc,(BrokenPipeError,ConnectionResetError,ConnectionAbortedError))
            or getattr(exc,'winerror',None) in (10053,10054)
            or exc.errno in (errno.EPIPE,errno.ECONNRESET,errno.ECONNABORTED))


def make_handler(app):
    class Handler(BaseHTTPRequestHandler):
        server_version = "IndustrialModelService/1.0"

        def handle(self):
            try:
                super().handle()
            except OSError as exc:
                if not _peer_disconnected(exc):
                    raise
                self.close_connection=True

        def _send(self, status, content_type, data):
            try:
                self._write_response(status,content_type,data)
            except OSError as exc:
                # A timed-out/closed client cannot receive another 500 response.
                # Ignore only known peer-disconnect errors, not application or
                # filesystem failures. Do not retry or alter a computed result.
                if _peer_disconnected(exc):
                    self.close_connection=True
                    return
                raise

        def _write_response(self, status, content_type, data):
            self.send_response(int(status))
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "GET,POST,OPTIONS")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self._send(204, "text/plain", b"")

        def do_GET(self):
            try:
                status, ctype, data = app.handle_get(urlparse(self.path).path)
                self._send(status, ctype, data)
            except JsonServiceError as exc:
                self._send(exc.status, "application/json; charset=utf-8", _json_bytes({"success": False, "error": exc.code, "message": str(exc), "details": exc.details}))
            except Exception as exc:
                self._send(500, "application/json; charset=utf-8", _json_bytes({"success": False, "error": "internal_error", "message": str(exc), "trace": traceback.format_exc()}))

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length > 20 * 1024 * 1024:
                    raise JsonServiceError("请求体超过20MB", 413, "payload_too_large")
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except Exception as exc:
                    raise JsonServiceError("请求JSON无效: %s" % exc, 400, "invalid_json")
                result = app.handle_post(urlparse(self.path).path, payload)
                self._send(200, "application/json; charset=utf-8", _json_bytes(result))
            except JsonServiceError as exc:
                self._send(exc.status, "application/json; charset=utf-8", _json_bytes({"success": False, "error": exc.code, "message": str(exc), "details": exc.details}))
            except Exception as exc:
                self._send(500, "application/json; charset=utf-8", _json_bytes({"success": False, "error": "internal_error", "message": str(exc), "trace": traceback.format_exc()}))

        def log_message(self, fmt, *args):
            print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    return Handler


def run_service(app, host="127.0.0.1", port=18101):
    server = ThreadingHTTPServer((host, int(port)), make_handler(app))
    print("%s listening on http://%s:%s" % (app.service_name, host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
