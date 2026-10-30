# -*- coding: utf-8 -*-
"""V21 UX configuration and Workbench boundaries."""
from __future__ import print_function

import json
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.configuration import load_model_service_config, load_service_portal_config
from app.server import Application, Handler
from app.store import Store
from run_app import available_port


def static_contracts():
    html = (ROOT / "app/static/index.html").read_text(encoding="utf-8")
    js = (ROOT / "app/static/app.js").read_text(encoding="utf-8")
    css = (ROOT / "app/static/styles.css").read_text(encoding="utf-8")
    effectiveness_js = (ROOT / "app/static/effectiveness.js").read_text(encoding="utf-8")
    price_js = (ROOT / "app/static/price.js").read_text(encoding="utf-8")
    generation_tasks = (ROOT / "app/generation_tasks.py").read_text(encoding="utf-8")
    server = (ROOT / "app/server.py").read_text(encoding="utf-8")
    for token in ("railResizeHandle", "hideCoveredParams", "demandSummary", "recommendBtn", "helpBtn"):
        assert token in html
    assert "推荐工作流程" not in html
    assert "parameterSearch" not in html
    assert "filter-search-results" in js and "selectParameter(" in js
    home_tour = js[js.index("function playTour(){"):js.index("function playDetailTour(){")]
    assert "saveSchemeBtn" not in home_tour
    assert "function tourScrollContainer(" in js and "scrollIntoView" in js and "container.scrollTo" in js
    assert "ipdemo-detail-tour-v21-complete" in js and "#saveSchemeBtn" in js
    assert 'id="generationCount" type="number" min="1" max="30" value="5"' in html
    assert 'generation_count:Number(q("generationCount").value||5)' in js
    assert "def canonicalize_generation_controls" in generation_tasks
    assert 'req.get("count") or req.get("generation_count") or 5' in generation_tasks
    assert "self.generation_tasks.canonicalize_generation_controls" in server
    assert 'required=field.required===false?"":" required"' in effectiveness_js
    assert 'required=f.required===false?"":" required"' in price_js
    assert "historical_incompatible_fallback" in effectiveness_js and "historical_incompatible_fallback" in price_js
    assert 'id="clearConditionBtn"' in html and "恢复默认" in html and "评价协议已恢复默认" in js
    assert 'localStorage.setItem(key,String(Math.round(value)))' in js
    assert 'key="ipdemo-search-rail-width"' in js
    assert "tag_parameter_coverage" in js
    assert "当前显式条件仍按用户输入优先" in js
    assert 'data-source="historical"' in html and 'data-source="generated"' in html and 'data-source="both"' in html
    assert 'source_mode:q("sourceMode").value' in js
    assert "--search-rail-width:390px" in css
    assert "min(680px,55vw)" in css
    assert "@media(max-width:900px)" in css and ".rail-resize-handle{display:none}" in css
    # Presentation state must never enter either request/fingerprint contract.
    payload_body = js[js.index("function requestPayload"):js.index("function parameter(")]
    assert "search-rail-width" not in payload_body and "railWidth" not in payload_body


def portal_and_login_contracts():
    portal = load_service_portal_config(ROOT)
    models = load_model_service_config(ROOT)
    assert list(portal["services"]) == ["recommendation", "quick_price", "cost_effectiveness_analysis", "advanced_price", "effectiveness", "admin"]
    assert portal["services"]["advanced_price"]["url"] == "/price/deep"
    assert portal["services"]["advanced_price"]["enabled"] is True
    assert portal["services"]["advanced_price"]["visible"] is True
    assert portal["services"]["cost_effectiveness_analysis"]["url"].endswith(":17000")
    assert portal["services"]["cost_effectiveness_analysis"]["url"].startswith("http://")  # Portal derives external/new-window from URL.
    assert "advanced_price" not in models
    assert models["price_service_url"].endswith(":18101")
    assert models["effectiveness_service_url"].endswith(":18102")
    assert Handler._safe_next("/admin?tab=data") == "/admin?tab=data"
    for unsafe in ("https://evil.test/", "//evil.test/", "/\\evil.test/", "admin", "javascript:alert(1)"):
        assert Handler._safe_next(unsafe) is None
    run_app = (ROOT / "run_app.py").read_text(encoding="utf-8")
    assert '"url": "http://%s:%d/portal"' in run_app

    with tempfile.TemporaryDirectory(prefix="ipdemo_portal_") as folder:
        config = Path(folder) / "config"
        config.mkdir()
        path = config / "service_portal.json"
        allowed = {
            "internal": {"label": "内部", "url": "/price", "enabled": True},
            "external_http": {"label": "HTTP", "url": "http://127.0.0.1:8080/", "enabled": True},
            "external_https": {"label": "HTTPS", "url": "https://example.test/tool", "enabled": True},
            "pending": {"label": "待配置", "url": "", "enabled": False},
        }
        path.write_text(json.dumps({"services": allowed}), encoding="utf-8")
        loaded = load_service_portal_config(folder)["services"]
        assert set(loaded) == set(allowed) | {"recommendation", "quick_price", "advanced_price", "effectiveness", "cost_effectiveness_analysis", "admin"}
        assert loaded["recommendation"]["url"] == "/"
        for unsafe in ("javascript:alert(1)", "data:text/html,x", "file:///tmp/x", "vbscript:x", "//evil.test/x", "\\\\evil.test\\x", ""):
            path.write_text(json.dumps({"services": {"bad": {"url": unsafe, "enabled": True}}}), encoding="utf-8")
            try:
                load_service_portal_config(folder)
                raise AssertionError("unsafe enabled Portal URL must fail: %s" % unsafe)
            except ValueError:
                pass
        path.write_text(json.dumps({"services": {"bad": {"url": "javascript:alert(1)", "enabled": False}}}), encoding="utf-8")
        try:
            load_service_portal_config(folder)
            raise AssertionError("dangerous disabled Portal URL must also fail")
        except ValueError:
            pass

    with tempfile.TemporaryDirectory(prefix="ipdemo_portal_save_", dir=str(ROOT)) as folder:
        config = Path(folder) / "config"
        config.mkdir()
        portal_path = config / "service_portal.json"
        model_path = config / "model_services.json"
        portal_path.write_text(json.dumps({"services": {"advanced_price": {"url": "", "enabled": False}}}), encoding="utf-8")
        model_path.write_text('{"sentinel":"unchanged"}', encoding="utf-8")
        app = Application.__new__(Application)
        app.root = Path(folder)
        app.portal_config = load_service_portal_config(folder)
        saved = app.save_portal_config({"services": {
            "advanced_price": {"url": "http://10.10.1.5:9000", "visible": True, "enabled": True}
        }})
        assert saved["saved"] is True and Path(saved["backup"]).is_file()
        assert app.portal_config["services"]["advanced_price"]["url"] == "http://10.10.1.5:9000"
        assert app.portal_config["services"]["recommendation"]["url"] == "/"
        assert model_path.read_text(encoding="utf-8") == '{"sentinel":"unchanged"}'


def standard_startup_contract():
    standard = (ROOT / "START_ALL_SERVICES_WIN7.bat").read_text(encoding="utf-8")
    no_browser = (ROOT / "START_ALL_NO_BROWSER.bat").read_text(encoding="utf-8")
    assert 'call START_RECOMMENDATION_WITH_SERVICES_WIN7.bat' in standard
    delegated = (ROOT / "START_RECOMMENDATION_WITH_SERVICES_WIN7.bat").read_text(encoding="utf-8")
    assert 'set IPDEMO_MODEL_EXECUTION_MODE=services' in delegated
    assert 'set IPDEMO_MODEL_SERVICE_FALLBACK=0' in delegated
    assert 'call START_ALL_NO_BROWSER.bat' in delegated
    assert "--no-browser" in no_browser
    assert "17891/portal" not in standard

    class FakeSocket(object):
        def bind(self, address):
            if address[1] == 17891:
                raise OSError("occupied")

        def close(self):
            pass

    with patch("run_app.socket.socket", side_effect=lambda *args, **kwargs: FakeSocket()):
        assert available_port("127.0.0.1", 17891, 1) == 17892


def deterministic_history_contract():
    with tempfile.TemporaryDirectory(prefix="ipdemo_v21_") as folder:
        db = Path(folder) / "v21.db"
        conn = sqlite3.connect(str(db))
        conn.executescript("""
          CREATE TABLE products(product_code TEXT PRIMARY KEY, enabled INTEGER);
          CREATE TABLE agreements(
            agreement_id TEXT PRIMARY KEY, product_code TEXT, agreement_name TEXT,
            agreement_source TEXT, source_year INTEGER, params_json TEXT,
            tags_json TEXT, enabled INTEGER, updated_at TEXT
          );
          INSERT INTO products VALUES('P',1);
          INSERT INTO agreements VALUES('AGR-Z','P','旧方案','historical',2023,'{"x":3}','[]',1,'2026-01-01');
          INSERT INTO agreements VALUES('AGR-B','P','同年后更新','imported',2024,'{"x":2}','[]',1,'2026-02-01');
          INSERT INTO agreements VALUES('AGR-A','P','同年先更新','historical',2024,'{"x":1}','[]',1,'2026-01-01');
        """)
        conn.commit()
        conn.close()
        store = Store(db, Path(folder) / "unused.csv", None, read_only=True)
        assert store.workbench_example()["agreement_id"] == "AGR-B"
        assert store.workbench_example("AGR-A")["agreement_id"] == "AGR-A"
        assert store.workbench_example("MISSING")["agreement_id"] == "AGR-B"


class FakeStore(object):
    def parameter_map(self):
        return {
            "flag": {
                "label": "是否具备该属性", "unit": "", "value_type": "boolean",
                "allowed_values_json": "[0,1]",
                "model_value_mapping_json": '{"0":-1,"1":1}',
                "display_value_mapping_json": '{"0":"无","1":"有"}',
            },
            "weight": {"label": "重量", "unit": "kg", "value_type": "number"},
        }

    def workbench_example(self, preferred=None):
        return {"agreement_id": "AGR-ONE", "agreement_name": "统一历史方案", "source_year": 2024,
                "parameters": {"weight": 4.2, "flag": 99}}

    def business_parameters(self, model_params, source_params=None):
        result = dict(source_params or {})
        result.update({key: (0 if value == -1 else value) for key, value in model_params.items() if key not in result})
        return result


def workbench_enrichment_contract():
    app = Application.__new__(Application)
    app.store = FakeStore()
    app.workbench_defaults = {"historical_example_agreement_id": ""}
    raw = {"fields": [
        {"field_name": "weight", "label": "model weight", "dtype": "number", "default_value": 8},
        {"field_name": "flag", "dtype": "number", "default_value": -1},
        {"field_name": "external_only", "label": "External Label", "dtype": "number", "training_mean": 9},
    ]}
    price = app._enrich_workbench_schema(raw, "price")
    effect = app._enrich_workbench_schema(raw, "effectiveness")
    assert price["example"]["agreement_id"] == effect["example"]["agreement_id"] == "AGR-ONE"
    fields = {field["field_name"]: field for field in price["fields"]}
    assert fields["weight"]["field_label"] == "重量" and fields["weight"]["example_value"] == 4.2
    assert fields["flag"]["example_value"] == 0
    assert fields["flag"]["allowed_values"] == [0, 1]
    assert fields["flag"]["example_source"] == "historical_incompatible_fallback"
    assert "历史值" in fields["flag"]["example_warning"]
    assert fields["external_only"]["field_label"] == "External Label"
    assert fields["external_only"]["example_value"] == 9
    assert json.loads(fields["flag"]["display_value_mapping_json"])["1"] == "有"


if __name__ == "__main__":
    static_contracts()
    portal_and_login_contracts()
    standard_startup_contract()
    deterministic_history_contract()
    workbench_enrichment_contract()
    print("PASS V21 workflow, portal, deterministic Workbench and value boundaries")
