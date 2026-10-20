# -*- coding: utf-8 -*-
from __future__ import print_function

import json
import os
import shutil
import socket
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from app.server import Application, create_server

LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def check(condition, message, report):
    if not condition:
        raise AssertionError(message)
    print("PASS - " + message)
    report["checks"].append(message)


def make_temp_root():
    holder = tempfile.TemporaryDirectory(prefix="ipdemo_v19_5_test_")
    root = Path(holder.name)
    (root / "app").mkdir(parents=True)
    shutil.copytree(str(ROOT / "app" / "static"), str(root / "app" / "static"))
    shutil.copytree(str(ROOT / "models"), str(root / "models"))
    shutil.copytree(str(ROOT / "data_master"), str(root / "data_master"))
    (root / "data").mkdir()
    dataset = ROOT / "data" / "virtual_protocol_dataset.csv"
    if dataset.exists():
        shutil.copy2(str(dataset), str(root / "data" / dataset.name))
    for name in ("backups", "uploads", "logs", "runtime", "exports"):
        (root / name).mkdir()
    return holder, root


def free_port():
    sock = socket.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close(); return port


def get_json(url):
    with LOCAL_OPENER.open(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    with LOCAL_OPENER.open(req, timeout=90) as response:
        return json.loads(response.read().decode("utf-8"))


def wait_task(manager, task_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = manager.get(task_id)
        if task and task.get("status") in ("completed", "failed"):
            return task
        time.sleep(0.08)
    raise AssertionError("智能生成任务超时")


def main():
    expected_version = (ROOT / "VERSION.txt").read_text(encoding="utf-8").splitlines()[0].split("-", 1)[0]
    report = {"version": expected_version, "status": "RUNNING", "checks": [], "started_at": time.strftime("%Y-%m-%d %H:%M:%S")}
    os.environ.pop("IPDEMO_DEMO_READ_ONLY", None)
    os.environ.pop("IPDEMO_AUTH_ENABLED", None)
    holder, root = make_temp_root()
    try:
        app = Application(root)
        bootstrap = app.bootstrap()
        check(bootstrap["counts"]["historical"] > 0, "DataMaster可初始化参考方案库", report)
        check(app.store.integrity_check()["ok"], "SQLite数据库完整", report)
        check(app.runtime.manifest()["contract_valid"], "价格与效能模型契约有效", report)
        exported = app.data_master.export_current()
        check(app.data_master.parse("DataMaster_Current.xlsx", exported)["valid"], "当前DataMaster可导出并重新校验", report)
        protection = app.store.parameter_map()["protection_grade"]
        check(protection.get("search_type") == "integer" and not protection.get("allowed_values_json"), "防护等级按整数合法域统一维护，不再被旧离散列表吸附", report)
        check(all(item.get("search_type") for item in bootstrap["parameters"]), "前端可获得统一的混合属性搜索类型", report)

        base_request = {
            "session_id": "quiet-session", "selected_tags": [], "indicator_filter_mode": "all",
            "indicator_filters": [], "sort_by": "comprehensive", "count": 2,
            "generation_count": 2, "source_mode": "historical", "page": 1, "page_size": 20,
        }
        initial = app.recommend(dict(base_request, start_generation=False))
        check(initial.get("generation_task") is None, "页面初始加载不启动生成", report)
        started = time.time()
        recommended = app.recommend(dict(base_request, start_generation=True))
        check(time.time() - started < 2.0 and recommended["items"], "开始智能推荐后立即返回已有参考方案", report)
        check(recommended.get("generation_task", {}).get("task_id"), "智能推荐点击后静默启动生成任务", report)
        first_task = wait_task(app.generation_tasks, recommended["generation_task"]["task_id"])
        check(first_task["status"] == "completed" and first_task.get("batch_id"), "静默生成完成并形成稳定批次", report)
        first_batch = first_task["batch_id"]
        first_items = app.sessions.get("quiet-session", first_batch)
        check(first_items and all(item.get("batch_id") == first_batch for item in first_items), "生成方案具有批次和候选稳定标识", report)
        old_id = first_items[0]["agreement_id"]

        second = app.generation_tasks.start(dict(base_request, session_id="quiet-session", count=2, seed=98765), force=True)
        second = wait_task(app.generation_tasks, second["task_id"])
        check(second["status"] == "completed" and second.get("batch_id") != first_batch, "再次生成创建新批次而不覆盖旧批次", report)
        check(app.agreement_detail(old_id, "quiet-session") is not None, "新批次完成后旧生成方案仍可打开编辑", report)

        generated_view = app.recommend(dict(base_request, source_mode="generated", generation_batch_id=second["batch_id"]))
        check(generated_view["total"] > 0 and all(x.get("is_generated") for x in generated_view["items"]), "智能生成方案可独立查看", report)
        both_view = app.recommend(dict(base_request, source_mode="both", generation_batch_id=second["batch_id"], page_size=200))
        check(both_view["total"] >= generated_view["total"] + initial["total"], "全部推荐方案同时包含参考与生成结果", report)

        price_request = {
            "session_id": "price-session", "selected_tags": [], "indicator_filter_mode": "all",
            "indicator_filters": [], "max_price": 10.0, "min_feasibility": 0.0,
            "sort_by": "price", "count": 4,
        }
        price_result = app.generator.generate(price_request, count=4, seed=20260728, budget=320)
        check(any(float(item["predicted_price_wan"]) <= 10.0 for item in price_result["candidates"]), "价格不高于10万元会主动引导搜索并得到相应方案", report)
        check(all((item.get("generation_trace") or {}).get("active_output_targets", {}).get("max_price") == 10.0 for item in price_result["candidates"]), "生成轨迹记录模型输出目标", report)
        protection_definition = app.store.parameter_map()["protection_grade"]
        protection_neighbors = app.generator._attribute_neighbors(65, protection_definition, std_value=1.0, step_scale=0.25, include_bounds=True)
        check(64 in [int(value) for value in protection_neighbors], "整数等级合法邻域包含历史样本未覆盖的IP64", report)

        # Artificial one-step reachability: all attributes except protection grade
        # are locked. The synthetic price can only cross the ceiling by lowering
        # protection grade, so the generator must discover that late attribute.
        original_callback = app.generator.evaluate_callback
        chosen = next(item for item in app.store.historical_agreements() if int(item["params"]["protection_grade"]) >= 65)
        locked_filters = []
        for key, value in chosen["params"].items():
            if key == "protection_grade":
                continue
            definition = app.store.parameter_map()[key]
            operator = "boolean_is" if definition.get("value_type") == "boolean" else "eq"
            locked_filters.append({"parameter_id": key, "operator": operator, "value1": value})
        def synthetic_price(params, base=None):
            result = original_callback(params, base)
            price = 4.0 + 0.1 * float(result["parameters"]["protection_grade"])
            result["predicted_price_wan"] = round(price, 6)
            result["price_interval_wan"] = [round(price - 0.2, 6), round(price + 0.2, 6)]
            result["cost_effectiveness"] = round(float(result["capability_score"]) / max(price, 1e-9), 6)
            return result
        app.generator.evaluate_callback = synthetic_price
        try:
            reachable_request = {
                "session_id": "one-step", "selected_tags": [], "indicator_filter_mode": "all",
                "indicator_filters": locked_filters, "max_price": 10.0, "min_feasibility": 0.0, "count": 2,
            }
            reachable = app.generator.generate(reachable_request, count=2, seed=314159, budget=240)
            check(any(float(item["predicted_price_wan"]) <= 10.0 and int(item["params"]["protection_grade"]) <= 60 for item in reachable["candidates"]), "人工单步可达的离散/整数降价路径必须被生成器发现", report)
        finally:
            app.generator.evaluate_callback = original_callback

        edited_source = price_result["candidates"][0]
        trace = edited_source.get("generation_trace") or {}
        edited_high = dict(edited_source["params"]); edited_high["protection_grade"] = 68
        evaluated_high = app.evaluate({
            "parameters": edited_high, "base_parameters": edited_source["params"],
            "base_agreement_id": trace.get("seed_agreement_id"), "base_tags": trace.get("seed_tags") or [],
            "recommendation_context": dict(trace.get("request_context") or {}, locked_parameters=trace.get("locked_parameters") or []),
        })
        edited_low = dict(edited_source["params"]); edited_low["protection_grade"] = 54
        evaluated_low = app.evaluate({
            "parameters": edited_low, "base_parameters": edited_source["params"],
            "base_agreement_id": trace.get("seed_agreement_id"), "base_tags": trace.get("seed_tags") or [],
            "recommendation_context": dict(trace.get("request_context") or {}, locked_parameters=trace.get("locked_parameters") or []),
        })
        check(evaluated_high["predicted_price_wan"] != evaluated_low["predicted_price_wan"], "编辑属性后价格评估随当前值变化", report)
        check("recommendation_assessment" in evaluated_high and "tag_evidence" in evaluated_high, "编辑后返回完整需求、标签、轮廓与风险重评估", report)
        check(evaluated_high["recommendation_assessment"] != evaluated_low["recommendation_assessment"] or evaluated_high["coupling_assessments"] != evaluated_low["coupling_assessments"], "右侧提示数据不再固定使用初始生成状态", report)

        rule_tag = next((tag for tag in bootstrap["tags"] if tag.get("derivation_mode") == "rule"), None)
        if rule_tag:
            tag_request = dict(base_request, selected_tags=[rule_tag["tag_id"]], count=2)
            tag_result = app.generator.generate(tag_request, count=2, seed=13579, budget=260)
            check(tag_result["candidates"], "标签条件可以生成候选方案", report)
            check(all("tag_evidence" in item for item in tag_result["candidates"]), "每份生成方案保存标签判定依据", report)
            check(any((item["tag_evidence"].get(rule_tag["tag_id"]) or {}).get("matched") for item in tag_result["candidates"]), "标签规则组被编译成生成目标并重新判定", report)

        impossible = {
            "session_id": "explore", "selected_tags": [], "indicator_filter_mode": "all",
            "indicator_filters": [], "max_price": 0.001, "min_capability": 99.99,
            "min_feasibility": 0.9999, "count": 1,
        }
        explore = app.generator.generate(impossible, count=1, seed=2468, budget=180)
        check(explore["candidates"] and explore["candidates"][0].get("best_effort"), "无严格解时仍返回明确标注的探索方案", report)

        save_evaluation = app.evaluate({"parameters": first_items[0]["params"]})
        changed_after_evaluation = dict(first_items[0]["params"])
        changed_after_evaluation["protection_grade"] = 54 if int(changed_after_evaluation["protection_grade"]) != 54 else 55
        try:
            app.save({"scheme_name":"过期结果不应保存","parameters":changed_after_evaluation,"evaluation_token":save_evaluation["evaluation_token"],"risk_confirmed":True})
            stale_rejected = False
        except ValueError:
            stale_rejected = True
        check(stale_rejected, "属性变化后不能使用旧计算结果保存", report)
        saved = app.save({"scheme_name": "V19.6测试方案", "base_agreement_id": old_id, "source_type": "expert_modified", "parameters": first_items[0]["params"], "evaluation_token":save_evaluation["evaluation_token"], "risk_confirmed": True})
        check(saved["saved"] and app.saved_detail(saved["scheme_id"]), "专家方案可保存并重新打开", report)

        index_html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        app_js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        check("generationWaitOverlay" not in index_html and "generationStatus" in index_html, "智能生成状态不再阻塞整个页面", report)
        check("您可以继续浏览、切换方案或调整页面" in app_js, "生成期间页面保持可操作", report)
        check("开始智能推荐" in index_html and "检索历史方案" not in index_html, "普通用户页面采用智能推荐产品表达", report)
        check("已有参考方案" in index_html and "智能生成方案" in index_html and "全部推荐方案" in index_html, "推荐结果支持三种查看范围", report)
        check("generationBasisPanel" in index_html and "tag_evidence" in app_js, "方案详情展示生成与标签依据", report)
        check("sessionStorage" in app_js, "浏览器刷新后保持生成会话标识", report)
        check("scheduleEvaluation" not in app_js and "markEvaluationDirty" in app_js and 'q("evaluateBtn").onclick=evaluate' in app_js, "方案参数变化只标记结果过期，点击按钮后才计算", report)
        check("evaluation_token" in app_js and "请先点击“重新计算价格与效能”" in app_js, "保存方案必须绑定用户已查看的最新显式计算结果", report)
        check("recommendation_assessment" in app_js and "e.coupling_assessments" in app_js, "右侧提示使用当前评估而非初始静态提示", report)
        check("ordered_discrete" in app_js and "integer" in app_js and "unordered_enum" in app_js, "前端统一支持连续、整数、有序离散、无序枚举和布尔属性", report)
        check("showPriceAttributes" in index_html and "showPriceAttributes" in app_js, "价格专用属性默认折叠并可由用户展开", report)
        check("价格＋效能" in app_js and "效能" in app_js and "价格" in app_js, "方案编辑区标识共享、效能专用和价格专用属性", report)
        check("parameter_roles" in bootstrap and "model_input_bindings" in bootstrap, "Bootstrap返回双模型字段角色和绑定", report)

        port = free_port(); server = create_server(root, "127.0.0.1", port)
        thread = threading.Thread(target=server.serve_forever); thread.daemon = True; thread.start()
        try:
            base = "http://127.0.0.1:%d" % port
            health = get_json(base + "/api/health")
            check(health["status"] == "ok" and health["version"] == expected_version, "单端口HTTP健康检查通过", report)
            task = post_json(base + "/api/generation/request", dict(base_request, session_id="http-session", count=1))
            deadline = time.time() + 60
            while time.time() < deadline:
                public = get_json(base + "/api/generation-tasks/" + task["task_id"])
                if public["status"] in ("completed", "failed"): break
                time.sleep(0.1)
            check(public["status"] == "completed" and public.get("batch_id"), "HTTP生成任务返回稳定批次", report)
            view = post_json(base + "/api/recommend", dict(base_request, session_id="http-session", source_mode="generated", generation_batch_id=public["batch_id"]))
            check(view["total"] > 0, "HTTP可读取指定智能生成批次", report)
            detail_id = view["items"][0]["agreement_id"]
            detail = get_json(base + "/api/agreements/" + detail_id + "?session_id=http-session")
            check(detail.get("agreement_id") == detail_id, "HTTP生成方案详情可稳定打开", report)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)

        report["status"] = "PASS"
    except Exception as exc:
        report["status"] = "FAIL"; report["error"] = "%s: %s" % (type(exc).__name__, exc); raise
    finally:
        report["finished_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        (ROOT / "logs").mkdir(exist_ok=True)
        (ROOT / "logs" / "full_pipeline_test_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        holder.cleanup()
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
