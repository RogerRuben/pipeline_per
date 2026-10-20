# -*- coding: utf-8 -*-
from __future__ import print_function

import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.server import Application
from services.common.http_service import ServiceApplication, make_handler


class ContractStub(ServiceApplication):
    def __init__(self, kind):
        self.kind = kind
        self.requests = []

    def schema(self):
        if self.kind == "price":
            fields = [
                {"field_name": "price_required", "dtype": "number", "required": True,
                 "default_value": 12.0, "source": "configured_context"},
                {"field_name": "attr_001", "dtype": "boolean", "required": True},
            ]
        else:
            fields = [
                {"field_name": "effect_required", "dtype": "number", "required": True,
                 "training_mean": 8.0},
                {"field_name": "attr_005", "dtype": "number", "required": True,
                 "generation_min": 8000, "generation_max": 15000},
            ]
        return {"product_code": "SCHEMA-%s" % self.kind.upper(), "product_name": self.kind,
                "backend": "contract_stub", "model_version": "1", "fields": fields}

    def handle_post(self, path, payload):
        self.requests.append(dict(payload))
        parameters = dict(payload.get("parameters") or {})
        if self.kind == "price":
            return {"success": True, "prediction": {"predicted_price_wan": 15.0,
                    "price_interval_wan": [14.0, 16.0]}, "input_status": {},
                    "domain_status": {"in_domain": True}, "model": {"backend": "stub"}}
        return {"success": True, "parameters": parameters,
                "evaluation": {"effectiveness_score": 88.0, "feasibility_probability": 0.9},
                "model": {"backend": "stub"}}


def start(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    return server, thread, "http://127.0.0.1:%d" % server.server_address[1]


def make_root():
    holder = tempfile.TemporaryDirectory(prefix="complete_payload_")
    root = Path(holder.name)
    for name in ("app", "models", "data_master"):
        shutil.copytree(str(ROOT / name), str(root / name))
    (root / "data").mkdir()
    source = ROOT / "data" / "virtual_protocol_dataset.csv"
    if source.exists():
        shutil.copy2(str(source), str(root / "data" / source.name))
    for name in ("backups", "uploads", "logs", "runtime", "exports"):
        (root / name).mkdir()
    return holder, root


def main():
    price = ContractStub("price")
    effect = ContractStub("effectiveness")
    pserver, pthread, purl = start(price)
    eserver, ethread, eurl = start(effect)
    holder, root = make_root()
    old = dict(os.environ)
    try:
        os.environ["IPDEMO_MODEL_EXECUTION_MODE"] = "services"
        os.environ["IPDEMO_PRICE_SERVICE_URL"] = purl
        os.environ["IPDEMO_EFFECT_SERVICE_URL"] = eurl
        os.environ["IPDEMO_MODEL_SERVICE_FALLBACK"] = "0"
        app = Application(root)

        # Reproduce the production failure: the first historical row contains
        # only one old field although DataMaster has many enabled definitions.
        conn = app.store.connect()
        try:
            code = app.store.current_product_code()
            row = conn.execute("SELECT agreement_id FROM agreements WHERE product_code=? AND enabled=1 ORDER BY agreement_id LIMIT 1", (code,)).fetchone()
            if row:
                conn.execute("UPDATE agreements SET params_json=? WHERE agreement_id=?",
                             (json.dumps({"attr_001": 1}), row["agreement_id"]))
            conn.commit()
        finally:
            conn.close()

        minimal = app.store.admin_upsert("parameters", {
            "label": "无默认值但启用的文本字段", "value_type": "text",
            "allowed_values_json": "[]", "model_value_mapping_json": "{}",
        })
        assert minimal["saved"]
        saved_tag = app.store.admin_upsert("tags", {"tag_name": "自动默认标签"})
        assert saved_tag["saved"]

        enabled = set(key for key, item in app.store.parameter_map().items() if int(item.get("enabled") or 0))
        example = app._example_parameters()
        assert enabled.issubset(set(example)), (sorted(enabled - set(example)), sorted(example))
        assert example["price_required"] == 12.0
        assert example["effect_required"] == 8.0

        readiness = app._refresh_model_data_readiness()
        assert readiness["ready"], readiness
        for stub in (price, effect):
            sent = (stub.requests[-1].get("parameters") or {})
            assert enabled.issubset(set(sent)), sorted(enabled - set(sent))
            assert sent["price_required"] == 12.0 and sent["effect_required"] == 8.0

        snapshot = app._model_service_snapshot()
        price_params = snapshot["request_examples"]["price"]["body"]["parameters"]
        effect_params = snapshot["request_examples"]["effectiveness"]["body"]["parameters"]
        assert price_params == effect_params
        coverage = snapshot["parameter_coverage"]
        assert coverage["missing_enabled_in_probe"] == []
        assert coverage["missing_price_schema_in_probe"] == []
        assert coverage["missing_effectiveness_schema_in_probe"] == []

        release = app.product_releases.create("SAVE-TEST", "保存测试")
        updated = app.product_releases.set_section(release["release_id"], "tags", [{"tag_name": "草稿自动编号"}])
        draft_tag = updated["data"]["tags"][0]
        assert draft_tag["tag_id"].startswith("TAG-") and draft_tag["weight"] == 1.0

        try:
            app.store.admin_upsert("parameters", {"label": "坏JSON", "allowed_values_json": "[bad"})
            raise AssertionError("invalid JSON should be rejected")
        except ValueError as exc:
            assert "JSON数组" in str(exc)

        print(json.dumps({"status": "PASS", "enabled_fields": len(enabled),
                          "payload_fields": len(price_params), "coverage": coverage}, ensure_ascii=False))
    finally:
        os.environ.clear(); os.environ.update(old)
        for server, thread in ((pserver, pthread), (eserver, ethread)):
            server.shutdown(); server.server_close(); thread.join(timeout=3)
        holder.cleanup()


if __name__ == "__main__":
    main()
