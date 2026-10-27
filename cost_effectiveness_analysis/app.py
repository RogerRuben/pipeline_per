# -*- coding: utf-8 -*-
from __future__ import print_function

import argparse
import json
import mimetypes
import os
import sys
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from .analysis import apply_analysis, summarize
from .config import load_config
from .db_reader import DatabaseUnavailable, ReadOnlySchemeRepository
from .model_client import CostEffectivenessModelClient


class CostEffectivenessApplication(object):
    def __init__(self, root=ROOT, repository=None, model_client=None, config=None):
        self.root = Path(root).resolve()
        self.config = config or load_config(self.root)
        self.repository = repository or ReadOnlySchemeRepository(self.config["database"]["path"])
        self.model_client = model_client or CostEffectivenessModelClient(
            self.config["services"]["price"]["url"],
            self.config["services"]["effectiveness"]["url"],
            self.config["timeout_seconds"],
            self.config.get("price_output"),
        )
        self.static_dir = Path(__file__).resolve().parent / "static"

    def health(self):
        try:
            self.repository.get_sources()
            database_status, database_error = "ok", None
        except Exception as exc:
            database_status, database_error = "unavailable", str(exc)
        services = self.model_client.health()
        return {
            "service": "cost-effectiveness-analysis", "status": "ok",
            "port": int(self.config["port"]), "database_readonly": True,
            "database": database_status, "database_error": database_error,
            "price_service": (services.get("price") or {}).get("status", "unavailable"),
            "effectiveness_service": (services.get("effectiveness") or {}).get("status", "unavailable"),
            "services": services,
            "price_output": self.config.get("price_output"),
            "recommendation_url": self.config.get('recommendation_url','http://127.0.0.1:17891/portal'),
        }

    def list_schemes(self, source=None, search=None):
        return {"items": self.repository.list_schemes(source=source, search=search),
                "sources": self.repository.get_sources(), "database_readonly": True}

    def scheme_detail(self, scheme_id):
        return self.repository.get_scheme(scheme_id)

    def analyze(self, request):
        scheme_ids = request.get("scheme_ids") if isinstance(request, dict) else None
        if not isinstance(scheme_ids, list):
            raise ValueError("scheme_ids必须是数组")
        scheme_ids = [str(x) for x in scheme_ids]
        if len(scheme_ids) < 2:
            raise ValueError("请选择至少两个方案开始分析。")
        if len(scheme_ids) > 30:
            raise ValueError("为保证图表可读性，一次最多选择30个方案。")
        if len(set(scheme_ids)) != len(scheme_ids):
            raise ValueError("方案列表中存在重复项。")
        schemes = []
        for scheme_id in scheme_ids:
            scheme = self.repository.get_scheme(scheme_id)
            if not scheme:
                raise KeyError("方案不存在：%s" % scheme_id)
            scheme["model_parameters"] = self.repository.get_scheme_parameters(scheme_id, model_values=True)
            schemes.append(scheme)
        evaluated = self.model_client.evaluate_batch(schemes, request.get("target_protocol"))
        by_id = dict((x["scheme_id"], x) for x in evaluated["items"])
        merged = []
        for scheme in schemes:
            item = dict(scheme)
            item.pop("model_parameters", None)
            item.update(by_id.get(scheme["scheme_id"], {}))
            merged.append(item)
        analyzed, frontier = apply_analysis(merged)
        return {
            "success": True, "analysis_id": "CEA-%s" % uuid.uuid4().hex[:12].upper(),
            "summary": summarize(analyzed), "schemes": analyzed,
            "pareto_scheme_ids": frontier, "models": evaluated["models"],
            "target_protocol": evaluated["target_protocol"],
            "analysis_time": evaluated["analysis_time"],
            "price_output": evaluated.get("price_output") or self.config.get("price_output"),
            "service_errors": evaluated["service_errors"],
        }


class Handler(BaseHTTPRequestHandler):
    app = None

    def log_message(self, fmt, *args):
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    def _json(self, payload, status=200):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, exc, status=500):
        if isinstance(exc, ValueError):
            status = 400
        elif isinstance(exc, KeyError):
            status = 404
        elif isinstance(exc, DatabaseUnavailable):
            status = 503
        self._json({"success": False, "error": exc.__class__.__name__,
                    "message": str(exc).strip("'\"")}, status)

    def _static(self, name):
        path = (self.app.static_dir / name).resolve()
        if path.parent != self.app.static_dir.resolve() or not path.is_file():
            self._json({"error": "not_found", "message": "资源不存在"}, 404)
            return
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", (mimetypes.guess_type(str(path))[0] or "application/octet-stream") + ("; charset=utf-8" if path.suffix in (".html", ".js", ".css") else ""))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        parsed = urlparse(self.path)
        try:
            if parsed.path in ("/", "/index.html"):
                return self._static("index.html")
            if parsed.path in ("/app.js", "/styles.css"):
                return self._static(parsed.path[1:])
            if parsed.path == "/health":
                return self._json(self.app.health())
            if parsed.path == "/api/schemes":
                query = parse_qs(parsed.query)
                return self._json(self.app.list_schemes(
                    (query.get("source") or [None])[0], (query.get("search") or [None])[0]
                ))
            if parsed.path.startswith("/api/schemes/"):
                scheme = self.app.scheme_detail(unquote(parsed.path.split("/api/schemes/", 1)[1]))
                return self._json({"scheme": scheme}, 200 if scheme else 404)
            self._json({"error": "not_found", "message": "接口不存在"}, 404)
        except Exception as exc:
            self._error(exc)

    def do_POST(self):
        parsed = urlparse(self.path)
        try:
            size = int(self.headers.get("Content-Length") or 0)
            if size > 1024 * 1024:
                raise ValueError("请求内容过大")
            request = json.loads(self.rfile.read(size).decode("utf-8") or "{}")
            if parsed.path == "/api/analyze":
                return self._json(self.app.analyze(request))
            self._json({"error": "not_found", "message": "接口不存在"}, 404)
        except Exception as exc:
            self._error(exc)


def create_server(root=ROOT, host=None, port=None, repository=None, model_client=None, config=None):
    config = config or load_config(root)
    application = CostEffectivenessApplication(root, repository, model_client, config)
    handler = type("CostEffectivenessHandler", (Handler,), {"app": application})
    return ThreadingHTTPServer((host or config["host"], int(port or config["port"])), handler)


def main():
    config = load_config(ROOT)
    parser = argparse.ArgumentParser(description="效费比分析工作台")
    parser.add_argument("--host", default=config["host"])
    parser.add_argument("--port", type=int, default=config["port"])
    args = parser.parse_args()
    config["host"], config["port"] = args.host, args.port
    server = create_server(ROOT, args.host, args.port, config=config)
    print("效费比分析工作台：http://%s:%d" % (args.host, args.port))
    print("推荐数据库以 SQLite URI mode=ro 打开；本服务不依赖 :17891。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
