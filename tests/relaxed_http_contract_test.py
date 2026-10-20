# -*- coding: utf-8 -*-
from __future__ import print_function

import json
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.model_service_client import ModelServiceGateway, ServiceBackedRuntime
from services.common.http_service import ServiceApplication, make_handler


class Stub(ServiceApplication):
    def __init__(self, kind, code):
        self.kind, self.code = kind, code

    def schema(self):
        return {
            "product_code": self.code,
            "product_name": self.code,
            "backend": "contract_stub",
            "fields": [{"field_name": "attr_001", "dtype": "number", "required": True}],
        }

    def handle_post(self, path, payload):
        candidate = payload.get("candidate_id")
        if self.kind == "price":
            one = {"candidate_id": candidate, "success": True,
                   "prediction": {"predicted_price_wan": 12.5, "price_interval_wan": [12, 13]},
                   "input_status": {}, "domain_status": {"in_domain": True},
                   "model": {"product_code": self.code, "model_version": "P", "backend": "stub"}}
        else:
            one = {"candidate_id": candidate, "success": True,
                   "parameters": payload.get("parameters") or {},
                   "evaluation": {"effectiveness_score": 101, "feasibility_probability": 0.9},
                   "model": {"product_code": self.code, "model_version": "E", "backend": "stub"}}
        if path.endswith("/batch"):
            return {"success": True, "items": [self.handle_post(path[:-6], item) for item in payload.get("items") or []]}
        return one


def start(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever); thread.daemon = True; thread.start()
    return server, thread, "http://127.0.0.1:%s" % server.server_address[1]


def main():
    pserver, pthread, purl = start(Stub("price", "PRICE-SCHEMA-OLD"))
    eserver, ethread, eurl = start(Stub("effect", "EFFECT-SCHEMA-OLD"))
    try:
        gateway = ModelServiceGateway(None, purl, eurl, timeout=5, fallback=False)
        runtime = ServiceBackedRuntime(gateway)
        result = runtime.evaluate({"attr_001": 1})
        assert result["predicted_price_wan"] == 12.5
        assert result["capability_score"] == 101.0
        batch = runtime.evaluate_batch([{"candidate_id": "A", "parameters": {"attr_001": 1}}])
        assert len(batch) == 1 and batch[0]["feasibility_probability"] == 0.9
        print(json.dumps({"status": "PASS", "message": "Schema/product_code差异不阻断标准HTTP JSON实算"}, ensure_ascii=False))
    finally:
        for server, thread in ((pserver, pthread), (eserver, ethread)):
            server.shutdown(); server.server_close(); thread.join(timeout=3)


if __name__ == "__main__":
    main()
