# -*- coding: utf-8 -*-
from __future__ import print_function

import ast
import io
import json
import math
import os
import shutil
import socket
import tempfile
import threading
import zipfile
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(ROOT))

from app.server import Application
from app.model_service_client import ModelServiceGateway
from services.common.http_service import make_handler
from services.price_service.app import PriceService
from services.effectiveness_service.app import EffectivenessService, SnapshotBackend, OriginalRuntimeBackend
from services.price_service.export_native_price_bundle import export_from_notebook
from services.price_service.native_bundle import load_bundle, predict as native_predict


def check(value, message, report):
    if not value:
        raise AssertionError(message)
    report["checks"].append(message)
    print("PASS - " + message)


def make_root():
    holder = tempfile.TemporaryDirectory(prefix="v196_test_")
    root = Path(holder.name)
    for name in ("app", "models", "data_master"):
        shutil.copytree(str(ROOT / name), str(root / name))
    (root / "data").mkdir()
    src = ROOT / "data" / "virtual_protocol_dataset.csv"
    if src.exists(): shutil.copy2(str(src), str(root / "data" / src.name))
    for name in ("backups", "uploads", "logs", "runtime", "exports"):
        (root / name).mkdir()
    return holder, root


def start_http(app):
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app))
    thread = threading.Thread(target=server.serve_forever); thread.daemon = True; thread.start()
    return server, thread, "http://127.0.0.1:%d" % server.server_address[1]


class Col(object):
    def __init__(self, values): self.values=list(values)
    def min(self): return min(self.values)
    def max(self): return max(self.values)
    def mean(self): return sum(self.values)/float(len(self.values))


class Frame(object):
    def __init__(self, names, values): self.columns=list(names); self.data=dict((name,Col([row[i] for row in values])) for i,name in enumerate(names))
    def __getitem__(self,key): return self.data[key]


def main():
    report={"version":"V19.6.5","checks":[],"status":"RUNNING"}
    os.environ.pop("IPDEMO_MODEL_EXECUTION_MODE", None)
    holder, root = make_root()
    servers=[]
    try:
        app=Application(root)
        snap=app.store.admin_snapshot()
        pids=[x["parameter_id"] for x in snap["parameters"]]
        base=snap["agreements"][0]

        # Lifecycle and saving under a disabled parent.
        app.store.admin_upsert("tags", {"tag_id":"TAG-V196","tag_name":"测试标签","tag_group":"测试","weight":1.0,"derivation_mode":"rule","description":"","enabled":1})
        app.store.admin_toggle("tags","TAG-V196",False)
        saved=app.store.admin_upsert("tag_rules", {"rule_id":"RULE-V196","tag_id":"TAG-V196","parameter_id":pids[0],"operator":"gte","value1":"0","value2":"","rule_group":"default","enabled":1})
        check(saved["saved"], "停用标签下的规则仍可编辑保存", report)
        app.store.admin_toggle("tag_rules","RULE-V196",False)
        app.store.admin_purge("tag_rules","RULE-V196")
        app.store.admin_purge("tags","TAG-V196")
        check("TAG-V196" not in app.store.tag_map(include_disabled=True), "归档后可在无引用时受控永久删除", report)

        app.store.admin_upsert("couplings", {"coupling_id":"CPL-V196","coupling_name":"测试关系","coupling_type":"positive","parameter_a":pids[0],"parameter_b":pids[1],"domain_operator":"gte","multiplier":1.0,"offset":0.0,"strength":0.2,"severity":"info","description":"","rationale":"test","display_order":999,"enabled":1})
        app.store.admin_toggle("couplings","CPL-V196",False); app.store.admin_purge("couplings","CPL-V196")
        app.store.admin_upsert("constraints", {"rule_id":"CONS-V196","rule_name":"测试约束","left_parameter":pids[1],"operator":"gte","right_parameter":pids[0],"multiplier":1.0,"offset":0.0,"severity":"warning","message":"","rationale":"test","display_order":999,"enabled":1})
        app.store.admin_toggle("constraints","CONS-V196",False); app.store.admin_purge("constraints","CONS-V196")
        check(True, "耦合和约束支持启停、归档和受控永久删除", report)

        agreement=dict(base)
        agreement.update({"agreement_id":"AGR-V196","agreement_name":"生命周期测试","enabled":1})
        app.store.admin_upsert("agreements", agreement)
        app.store.admin_delete("agreements","AGR-V196")
        check(any(x["agreement_id"]=="AGR-V196" and not int(x["enabled"]) for x in app.store.admin_snapshot()["agreements"]), "协议普通删除改为可恢复归档", report)
        app.store.admin_purge("agreements","AGR-V196")

        html=(ROOT/"app/static/admin.html").read_text(encoding="utf-8")
        js=(ROOT/"app/static/admin.js").read_text(encoding="utf-8")
        check('form="adminEditForm"' in html and 'saveAdminEdit' in js, "编辑保存按钮真实绑定管理表单", report)
        check('/api/admin/toggle' in js and '/api/admin/purge' in js, "管理列表提供启停和永久删除治理入口", report)

        # DataMaster guidance and validations.
        exported=app.data_master.export_current()
        parsed=app.data_master.parse("DataMaster_Current.xlsx", exported)
        check(parsed["valid"], "增强DataMaster可导出并重新导入", report)
        with zipfile.ZipFile(io.BytesIO(exported)) as zf:
            workbook=zf.read("xl/workbook.xml").decode("utf-8")
            xml="\n".join(zf.read(name).decode("utf-8") for name in zf.namelist() if name.startswith("xl/worksheets/sheet"))
        check("填写说明" in workbook and "字典_下拉项" in workbook and "definedNames" in workbook, "DataMaster包含填写说明、字典和动态引用范围", report)
        check("dataValidations" in xml and "DM_BOOLEAN_VALUES" in xml and "DM_RULE_FIELDS" in xml, "DataMaster提供类型、布尔值和引用字段下拉", report)
        template=app.data_master.template()
        check(app.data_master.parse("DataMaster_Template.xlsx", template)["valid"], "模板复用当前业务字段并可直接回导", report)

        # Portable service/gateway closed loop using current product.
        price=PriceService(None, ROOT/"models/price_bundle.json")
        effect=EffectivenessService(SnapshotBackend(ROOT/"models/effectiveness_bundle.json"))
        price_demo=price._one(price.example_request())
        effect_demo=effect._one(effect.example_request())
        check(price_demo.get("success") and effect_demo.get("success"), "两个模型服务的简易前端示例请求可直接执行", report)
        pserver,pthread,purl=start_http(price); eserver,ethread,eurl=start_http(effect); servers += [(pserver,pthread),(eserver,ethread)]
        gateway=ModelServiceGateway(app.local_runtime, purl, eurl, timeout=10, fallback=False)
        params=dict(base["params"])
        remote=gateway.evaluate(params); local=app.local_runtime.evaluate(params)
        check(abs(remote["predicted_price_wan"]-local["predicted_price_wan"]) < 1e-6, "价格服务与当前本地模型闭环一致", report)
        check(abs(remote["capability_score"]-local["capability_score"]) < 1e-6, "效能服务与当前本地模型闭环一致", report)
        batch=gateway.evaluate_batch([{"candidate_id":"A","parameters":params},{"candidate_id":"B","parameters":params}])
        check(len(batch)==2 and all(x["model_source"]=="independent_http_model_services" for x in batch), "两个独立服务支持推荐生成批量评价", report)
        os.environ["IPDEMO_MODEL_EXECUTION_MODE"]="services"
        os.environ["IPDEMO_PRICE_SERVICE_URL"]=purl
        os.environ["IPDEMO_EFFECT_SERVICE_URL"]=eurl
        os.environ["IPDEMO_MODEL_SERVICE_FALLBACK"]="0"
        service_application=Application(root)
        service_result=service_application.runtime.evaluate(params)
        check(service_result.get("model_source")=="independent_http_model_services", "完整Application可切换为独立服务执行模式", report)
        check(service_application.local_runtime is None and service_application.runtime.contract_version=="service-schema-1.1", "正式服务模式直接使用服务Schema且不加载本地bundle", report)
        remote_manifest=service_application.runtime.manifest()
        price_schema=service_application.price_workbench_schema()
        price_workbench=service_application.price_workbench_predict({"parameters":params})
        check(bool(price_schema.get("fields")) and float(price_workbench["prediction"]["predicted_price_wan"])>0, "独立价格工作台按HTTP字段完成单独预测", report)
        check(price_workbench["workbench"]["price_service_only"] and not price_workbench["workbench"]["effectiveness_service_called"], "价格工作台不触发效能服务或历史库写入", report)
        check((ROOT/"app/static/price.html").is_file() and (ROOT/"app/static/price.js").is_file(), "独立价格预测页面及脚本已纳入应用", report)
        check(remote_manifest["price"].get("backend")=="portable_json" and remote_manifest["effectiveness"].get("backend")=="snapshot_json", "主系统状态明确展示两个服务的实际backend", report)
        app_batch=service_application._evaluate_batch_with_rules([
            {"candidate_id":"APP-A","parameters":params,"base_parameters":params},
            {"candidate_id":"APP-B","parameters":params,"base_parameters":params},
        ])
        check(len(app_batch)==2 and service_application.generator.evaluate_batch_callback is not None, "主系统生成器已接入独立服务批量评价", report)
        for key in ("IPDEMO_MODEL_EXECUTION_MODE","IPDEMO_PRICE_SERVICE_URL","IPDEMO_EFFECT_SERVICE_URL","IPDEMO_MODEL_SERVICE_FALLBACK"):
            os.environ.pop(key,None)

        # Original effectiveness runtime is executed, not approximated.
        source=ROOT/"services/effectiveness_service/original_runtime_demo"
        original=OriginalRuntimeBackend(source, source/"data/aircraft_door_lock_demo.xlsx")
        scheme=original.app.project.schemes[0]
        values=dict(scheme.params)
        direct=original.app.evaluate(values, original.state)
        served=EffectivenessService(original)._one({"request_id":"E1","parameters":values})
        check(abs(float(served["evaluation"]["effectiveness_score"])-float(direct["effectiveness_score"])) < 1e-10, "效能服务直接复用原工程最终效能逻辑", report)
        check(abs(float(served["evaluation"]["feasibility_probability"])-float(direct["learned_feasibility_probability"])) < 1e-10, "效能服务直接复用原工程可行概率", report)

        # Native pickle exact price pipeline with no joblib dependency.
        import numpy as np
        from sklearn.preprocessing import MinMaxScaler
        from sklearn.linear_model import Ridge
        from sklearn.svm import SVR
        from sklearn.ensemble import GradientBoostingRegressor
        X=np.asarray([[1,10],[2,11],[3,15],[4,18],[5,22],[6,25],[7,28],[8,31]],dtype=float)
        y=np.log(np.asarray([8.0,8.4,9.2,10.1,11.3,12.4,13.2,14.1],dtype=float))
        scaler=MinMaxScaler().fit(X); XS=scaler.transform(X)
        ridge=Ridge(alpha=.1).fit(XS,y); svr=SVR(C=10,gamma="scale").fit(XS,y); gbdt=GradientBoostingRegressor(random_state=1,n_estimators=20).fit(XS,y)
        namespace={"X_train":Frame(["载荷","批量"],X),"scaler":scaler,"ridge_model":ridge,"svr_model":svr,"gbdt_model":gbdt,"weights":[.2,.3,.5]}
        bundle_path=root/"price_native_bundle.pkl"
        export_from_notebook(namespace, output=bundle_path, product_code="TEST", target_divisor_to_wan=1, field_metadata={"载荷":{"field_name":"load"},"批量":{"field_name":"quantity"}}, ensemble_model_names=["ridge","svr","gbdt"], ensemble_weights=[.2,.3,.5], strict=True)
        bundle=load_bundle(bundle_path)
        params2={"load":4.5,"quantity":20}
        result=native_predict(bundle,params2)
        xx=scaler.transform([[4.5,20]])
        expected=.2*math.exp(float(ridge.predict(xx)[0]))+.3*math.exp(float(svr.predict(xx)[0]))+.5*math.exp(float(gbdt.predict(xx)[0]))
        check(abs(result["predicted_price_wan"]-expected)<1e-6, "原生pickle价格服务保存Scaler、原模型、字段顺序和集成公式并等价预测", report)
        source_text=(ROOT/"services/price_service/native_bundle.py").read_text(encoding="utf-8")
        check("import joblib" not in source_text and "pickle.dump" in source_text, "价格原生模型包不依赖joblib", report)

        # Python 3.8 syntax and service docs.
        for folder in (ROOT/"app",ROOT/"services",ROOT/"tools"):
            for path in folder.rglob("*.py"):
                ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path), feature_version=(3,8))
        check(True, "应用、服务和工具通过Python 3.8语法解析", report)
        check((ROOT/"docs/api/MODEL_SERVICES_API.md").is_file() and price.openapi()["openapi"]=="3.0.3" and effect.openapi()["openapi"]=="3.0.3", "接口文档、OpenAPI和简易前端齐全", report)
        report["status"]="PASS"
    finally:
        for server,thread in servers:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
        holder.cleanup()
        (ROOT/"logs").mkdir(exist_ok=True)
        (ROOT/"logs/v19_6_service_governance_report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
        print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == "__main__": main()
