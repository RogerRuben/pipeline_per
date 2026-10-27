# -*- coding: utf-8 -*-
from __future__ import print_function

import json
import sqlite3
import threading

from app.generation_profiles import apply_generation_profile
from app.generation_tasks import GenerationTaskManager
from app.ranking_explanation import annotate_ranking_explanations
from app.recommender import rank_agreements
from app.relaxation_advisor import build_relaxation_suggestions
from app.requirement_versions import RequirementVersionService, canonical_demand, demand_fingerprint
from app.semantic_snapshot import canonicalize_snapshot, semantic_signature
from app.data_master import DataMasterService
from app.product_releases import ProductReleaseService
from app.store import Store
from app.xlsx_utils import read_workbook_bytes, write_workbook_bytes


class VersionStore(object):
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        conn = self.connect()
        conn.executescript("""
        CREATE TABLE requirement_versions(
          id INTEGER PRIMARY KEY AUTOINCREMENT, product_code TEXT, version_no INTEGER,
          parent_version_id INTEGER, demand_json TEXT, demand_fingerprint TEXT,
          change_summary_json TEXT, target_protocol TEXT, created_at TEXT, created_by TEXT,
          UNIQUE(product_code,version_no));
        """)
        conn.close()

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def current_product_code(self):
        return "P"


def test_demand_version_excludes_presentation_and_generation_controls(tmp_path):
    a = {"scenario": "cost", "max_price": 15, "sort_by": "price",
         "generation_budget": 100, "page": 1}
    b = dict(a, sort_by="capability", generation_budget=900, page=4)
    assert canonical_demand(a) == canonical_demand(b)
    assert demand_fingerprint(a) == demand_fingerprint(b)
    service = RequirementVersionService(VersionStore(tmp_path / "version.db"))
    v1 = service.capture(a)
    again = service.capture(b)
    assert v1["id"] == again["id"]
    v2 = service.capture(dict(a, max_price=14))
    assert v2["version_no"] == 2
    restored = service.restore(v1["id"])
    assert restored["version_no"] == 3
    assert restored["parent_version_id"] == v1["id"]


def test_backend_generation_profiles_and_custom_override():
    standard = apply_generation_profile({})
    assert standard["exploration_profile"] == "standard"
    assert standard["generation_budget"] == 360
    custom = apply_generation_profile({"exploration_profile": "quick", "generation_budget": 222})
    assert custom["generation_rounds"] == 4
    assert custom["generation_budget"] == 222
    assert custom["effective_exploration_profile"] == "custom"
    assert apply_generation_profile(standard) == standard


def test_generation_profile_canonicalization_is_fingerprint_stable():
    class Runtime(object):
        schema = {"product_code": "P"}
        def manifest(self):
            return {"model_versions": {"price": "p", "effectiveness": "e"}}
    class Store(object):
        def generation_semantics_fingerprint(self):
            return "s"
    class App(object):
        runtime, store = Runtime(), Store()
        def generation_budget_limit(self): return 2400
        def generation_rounds_limit(self): return 15
    manager = GenerationTaskManager(App())
    once = manager.canonicalize_generation_controls({"exploration_profile": "standard"})
    twice = manager.canonicalize_generation_controls(once)
    assert once == twice
    assert manager.fingerprint(once) == manager.fingerprint(twice)


def test_relative_ranking_explanation_uses_business_gap():
    definition = {"label": "重量", "unit": "kg"}
    first = {"agreement_id": "A", "strict_filter_satisfied": True,
             "predicted_price_wan": 15, "capability_score": 105,
             "requirement_assessment": {"conditions": [{"key": "weight", "label": "重量",
                 "matched": True, "business_gap": 0, "unit": "kg"}]}}
    second = {"agreement_id": "B", "strict_filter_satisfied": False,
              "predicted_price_wan": 12, "capability_score": 100,
              "requirement_assessment": {"conditions": [{"key": "weight", "label": "重量",
                  "matched": False, "business_gap": .7, "normalized_gap": .389, "unit": "kg"}]}}
    annotate_ranking_explanations([first, second], {"scenario": "performance"}, {"weight": definition})
    text = json.dumps(first["ranking_explanation"], ensure_ascii=False)
    assert "0.7kg" in text
    assert "0.389" not in text
    assert "完整满足当前需求" in text


def test_relative_ranking_explanation_uses_display_mapping_for_special_state():
    definition = {"label": "状态", "special_value_keys_json": '["-1"]',
                  "display_value_mapping_json": '{"-1":"无该属性","1":"有"}'}
    first = {"agreement_id": "A", "strict_filter_satisfied": True,
             "requirement_assessment": {"conditions": [{"key": "state", "label": "状态",
                 "matched": True, "actual": 1, "business_gap": None}]}}
    second = {"agreement_id": "B", "strict_filter_satisfied": False,
              "requirement_assessment": {"conditions": [{"key": "state", "label": "状态",
                  "matched": False, "actual": -1, "business_gap": None}]}}
    annotate_ranking_explanations([first, second], {}, {"state": definition})
    text = json.dumps(first["ranking_explanation"], ensure_ascii=False)
    assert "本方案：有" in text and "下一名：无该属性" in text
    factor_text = " ".join(item["text"] for item in first["ranking_explanation"]["factors"])
    assert "1 vs -1" not in factor_text


def test_ranking_explanation_uses_actual_user_override_trace():
    request = {"sort_by": "price", "sort_order": "asc", "sort_source": "user_override"}
    items = [
        {"agreement_id": "A", "agreement_source": "historical", "historical_price_wan": 10,
         "predicted_price_wan": 10, "capability_score": 80, "params": {}, "tags": []},
        {"agreement_id": "B", "agreement_source": "historical", "historical_price_wan": 12,
         "predicted_price_wan": 12, "capability_score": 100, "params": {}, "tags": []},
    ]
    ranked = rank_agreements(items, request, {}, definitions={}, tag_map={}, constraint_rules=[])
    annotate_ranking_explanations(ranked, request, {})
    trace = ranked[0]["ranking_trace"]
    assert trace["sort_key"] == "price" and trace["sort_source"] == "user_override"
    summary = ranked[0]["ranking_explanation"]["summary"]
    assert "价格" in summary and "用户调整" in summary
    assert "性能优先" not in summary


def test_relaxation_is_structured_and_bound_to_demand():
    request = {"max_price": 10, "indicator_filters": [], "selected_tags": []}
    candidates = [{"predicted_price_wan": 12, "capability_score": 80, "params": {}, "tags": []}]
    version = {"id": 4, "demand_fingerprint": demand_fingerprint(request)}
    suggestions = build_relaxation_suggestions(request, candidates, {}, {}, [], version)
    assert suggestions[0]["before"] == 10
    assert suggestions[0]["after"] == 12
    assert suggestions[0]["business_delta"] == 2
    assert suggestions[0]["demand_version_id"] == 4
    assert suggestions[0]["apply_patch"] == {"max_price": 12}


def test_relaxation_skips_nearest_threshold_that_still_has_no_strict_result():
    request = {"max_price": 10, "min_capability": 90,
               "indicator_filters": [], "selected_tags": []}
    candidates = [
        {"predicted_price_wan": 12, "capability_score": 80, "params": {}, "tags": []},
        {"predicted_price_wan": 14, "capability_score": 100, "params": {}, "tags": []},
    ]
    suggestions = build_relaxation_suggestions(request, candidates, {}, {}, [])
    price = next(item for item in suggestions if item["condition_id"] == "max_price")
    assert price["after"] == 14
    assert price["current_pool_new_strict_count"] == 1


def test_relaxation_strict_less_than_uses_display_precision_step():
    definitions = {"weight": {"label": "重量", "unit": "kg", "value_type": "number",
                               "search_type": "continuous", "decimal_places": 3}}
    request = {"indicator_filters": [{"parameter_id": "weight", "operator": "lt", "value1": 3.5}],
               "selected_tags": []}
    candidates = [{"params": {"weight": 4.2}, "tags": []}]
    suggestions = build_relaxation_suggestions(request, candidates, definitions, {}, [])
    suggestion = next(item for item in suggestions if item["condition_id"] == "weight")
    assert abs(suggestion["after"] - 4.201) < 1e-9
    assert suggestion["current_pool_new_strict_count"] == 1


def test_semantic_signature_preserves_three_layer_value_semantics():
    source = {"parameters": [{
        "parameter_id": "state", "label": "状态", "value_type": "enum",
        "search_type": "unordered_enum", "allowed_values_json": "[0,1,\"01\"]",
        "special_value_keys_json": "[-1]",
        "display_value_mapping_json": '{"-1":"无该属性","0":"无","1":"有","01":"编号01"}',
        "model_value_mapping_json": '{"-1":2,"0":0,"1":1,"01":"M01"}',
    }]}
    canonical = canonicalize_snapshot(source)
    parameter = canonical["parameters"][0]
    assert parameter["allowed_values"] == [0, 1, "01"]
    assert parameter["special_value_keys"] == [-1]
    assert parameter["model_value_mapping"]["01"] == "M01"
    assert semantic_signature(source) == semantic_signature(source)


def test_datamaster_value_mappings_sheet_roundtrip(tmp_path):
    class Runtime(object):
        schema = {"product_code": "P", "product_name": "P"}
        def manifest(self): return {"calculation_available": False}
        def feature_roles(self): return {"shared_features": [], "effectiveness_only_features": [], "price_only_features": []}
        def all_feature_specs(self): return []
    store = Store(tmp_path / "dm.db", tmp_path / "missing.csv", Runtime())
    store.replace_from_datamaster({
        "products": [{"product_code": "P", "product_name": "P"}],
        "parameters": [{
            "parameter_id": "state", "label": "状态", "value_type": "enum",
            "search_type": "unordered_enum", "allowed_values_json": '[0,1,"01"]',
            "special_value_keys_json": '["-1"]',
            "display_value_mapping_json": '{"-1":"无该属性","0":"无","1":"有","01":"编号01"}',
            "model_value_mapping_json": '{"-1":2,"0":0,"1":1,"01":"M01"}',
        }],
        "parameter_groups": [{"group_name": "其他"}], "tags": [], "tag_rules": [],
        "couplings": [], "constraints": [], "agreements": [], "model_inputs": [],
    }, evaluate_agreements=False, sync_model_contract=False)
    service = DataMasterService(store, Runtime())
    signature_before = semantic_signature(store.admin_snapshot())
    report = service.parse("roundtrip.xlsx", service.export_current())
    assert report["valid"], report["errors"]
    parameter = report["data"]["parameters"][0]
    assert json.loads(parameter["allowed_values_json"]) == [0, 1, "01"]
    assert json.loads(parameter["special_value_keys_json"]) == ["-1"]
    assert json.loads(parameter["display_value_mapping_json"])["-1"] == "无该属性"
    assert json.loads(parameter["model_value_mapping_json"])["01"] == "M01"
    assert semantic_signature(report["data"]) == signature_before, (
        canonicalize_snapshot(store.admin_snapshot()), canonicalize_snapshot(report["data"])
    )
    releases = ProductReleaseService(store, Runtime(), service, lambda: {"price_output": {"unit": "wan_yuan"}})
    draft = releases.clone_current()
    imported = releases.import_maintenance_workbook(
        draft["release_id"], "maintenance.xlsx", service.export_current())
    released_parameter = imported["release"]["data"]["parameters"][0]
    assert json.loads(released_parameter["display_value_mapping_json"])["-1"] == "无该属性"
    assert json.loads(released_parameter["model_value_mapping_json"])["01"] == "M01"
    package = json.loads(releases.export_package(draft["release_id"]).decode("utf-8"))
    assert package["format"] == "industrial-product-release-1.1"
    assert package["semantic_schema_version"] == "2"
    assert package["semantic_signature"]
    assert package["runtime_contract"]["price_output"]["unit"] == "wan_yuan"
    assert package["semantic_contract"]["model_bound"] == "derived_from_model_input_bindings"

    workbook = read_workbook_bytes(service.export_current())
    partial = write_workbook_bytes([
        ("指标定义", workbook["指标定义"]),
        ("ValueMappings", workbook["ValueMappings"]),
    ])
    partial_result = releases.import_maintenance_workbook(
        draft["release_id"], "partial-maintenance.xlsx", partial)
    assert partial_result["release"]["data"]["parameter_groups"][0]["group_name"] == "其他"
    partial_parameter = partial_result["release"]["data"]["parameters"][0]
    assert json.loads(partial_parameter["special_value_keys_json"]) == ["-1"]
    assert json.loads(partial_parameter["display_value_mapping_json"])["01"] == "编号01"


def test_relaxation_quantizes_non_grid_values_in_the_safe_direction():
    definition = {"label": "重量", "value_type": "number",
                  "search_type": "continuous", "decimal_places": 3}
    candidates = [{"params": {"weight": 4.2004}, "tags": []}]
    expected = {"lte": 4.201, "lt": 4.201, "gte": 4.2, "gt": 4.2}
    starts = {"lte": 3.5, "lt": 3.5, "gte": 5.0, "gt": 5.0}
    for operator, threshold in expected.items():
        request = {"indicator_filters": [{"parameter_id": "weight", "operator": operator,
                                            "value1": starts[operator]}], "selected_tags": []}
        suggestions = build_relaxation_suggestions(
            request, candidates, {"weight": definition}, {}, [])
        suggestion = next(item for item in suggestions if item["condition_id"] == "weight")
        assert abs(suggestion["after"] - threshold) < 1e-9
        assert suggestion["current_pool_new_strict_count"] == 1

    integer_definition = dict(definition, search_type="integer", decimal_places=0)
    integer_candidates = [{"params": {"weight": 4}, "tags": []}]
    for operator, threshold in {"lte": 4, "lt": 5, "gte": 4, "gt": 3}.items():
        request = {"indicator_filters": [{"parameter_id": "weight", "operator": operator,
                                            "value1": 3 if operator in ("lte", "lt") else 5}],
                   "selected_tags": []}
        suggestion = build_relaxation_suggestions(
            request, integer_candidates, {"weight": integer_definition}, {}, [])[0]
        assert suggestion["after"] == threshold


def test_full_ranked_analysis_and_authoritative_final_decision_contracts_are_wired():
    server = open("app/server.py", "r", encoding="utf-8").read()
    client = open("app/static/app.js", "r", encoding="utf-8").read()
    assert '"analysis_items": analysis_items' in server
    assert "for item in ranked[:100]" in server
    assert "state.analysisItems=data.analysis_items||data.items||[]" in client
    assert "var items=state.analysisItems.filter" in client
    assert "comparisonItemCache" in client
    final_client = client[client.index("async function markFinalDecision"):client.index("function setFont")]
    assert "scheme_snapshot" not in final_client
    assert "never trust a client supplied snapshot" in server
    assert "需求版本不属于当前成品" in server
    assert "schemeDirty=false" in client
    assert "state.schemeDirty=true" in client
    assert "if(state.schemeDirty)return toast" in client
    assert "state.evaluationDirty=false;renderEvaluation" in client
