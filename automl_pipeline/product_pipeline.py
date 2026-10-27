# -*- coding: utf-8 -*-
"""One product table -> price AutoML -> trial effectiveness -> recommendation draft.

Builds in an isolated directory. Existing installed models and business data
are never part of the build output. Python 3.8 / sklearn 0.24 is supported.
"""
from __future__ import print_function

import argparse
import csv
import hashlib
import io
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.historical_onboarding import HistoricalProductOnboarding
from app.product_table import product_table_rows
from app.product_releases import ProductReleaseService
from app.store import Store
from app.model_service_client import DegradedServiceRuntime


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def virtual_fixture(rows=160, seed=20261002):
    rng = random.Random(seed)
    labels = ["成品编号", "成品名称", "价格(万元)", "是否应急解锁", "是否自检", "额定推力(N)",
              "峰值推力(N)", "运行速度(mm/s)", "质量(kg)", "响应时间(ms)", "工作温度下限(℃)",
              "工作温度上限(℃)", "防护等级(IP)", "盐雾试验时间(h)", "电源类型", "标签"]
    ids = ["product_id", "product_name", "price_wan", "emergency_unlock", "self_test", "rated_thrust_n",
           "peak_thrust_n", "speed_mm_s", "mass_kg", "response_time_ms", "temp_low_c", "temp_high_c",
           "protection_grade", "salt_spray_h", "power_type", "tags"]
    body = []
    for i in range(rows):
        thrust = round(rng.uniform(4000, 18000), 1)
        speed = round(rng.uniform(10, 85), 2)
        peak = round(thrust * 1.45 + rng.uniform(-250, 250), 1)
        mass = round(2.2 + thrust * .00038 + speed * .018 + rng.uniform(-.2, .2), 2)
        response = round(330 - speed * 2.7 + rng.uniform(-12, 12), 1)
        emergency, self_test = rng.randrange(2), rng.randrange(2)
        grade = rng.choice([54, 55, 65, 66, 67])
        salt = rng.choice([24, 48, 96, 168, 240])
        power = rng.choice(["24V", "48V"])
        price = round(3 + .00042 * thrust + .016 * speed + .03 * mass + .6 * emergency + .35 * self_test
                      + .0025 * salt + .03 * (grade - 54) + (.8 if power == "48V" else 0) + rng.gauss(0, .08), 4)
        body.append(["V%03d" % (i + 1), "虚拟执行机构%03d" % (i + 1), price, "有" if emergency else "无",
                     "有" if self_test else "无", thrust, peak, speed, mass,
                     "" if i % 19 == 0 else response, rng.randint(-55, -20), rng.randint(65, 100),
                     "IP%d" % grade, salt, power, "环境适应" if grade >= 65 else "常规应用"])
    # Repeated configurations verify grouped splitting; IDs and names are distinct.
    for i in range(4):
        duplicate = list(body[i])
        duplicate[0], duplicate[1] = "VD%03d" % i, "虚拟重复配置%03d" % i
        body.append(duplicate)
    config = {"attributes": {
        "emergency_unlock": {"preference": "higher", "weight": 1}, "self_test": {"preference": "higher", "weight": 1},
        "rated_thrust_n": {"preference": "higher", "weight": 2}, "peak_thrust_n": {"preference": "higher", "weight": 1},
        "speed_mm_s": {"preference": "higher", "weight": 1}, "mass_kg": {"preference": "lower", "weight": 1},
        "response_time_ms": {"preference": "lower", "weight": 1}, "temp_low_c": {"preference": "lower", "weight": 1},
        "temp_high_c": {"preference": "higher", "weight": 1}, "protection_grade": {"preference": "higher", "weight": 1},
        "salt_spray_h": {"preference": "higher", "weight": 1}},
              "couplings": [{"source": "rated_thrust_n", "target": "peak_thrust_n"},
                            {"source": "rated_thrust_n", "target": "mass_kg"},
                            {"source": "speed_mm_s", "target": "response_time_ms"}]}
    return {"labels": labels, "field_ids": ids, "rows": body, "effectiveness_config": config,
            "virtual_data": True, "data_source": "确定性软件流程验证数据，不代表真实成品"}


def fixture_csv(fixture):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerows([fixture["labels"], fixture["field_ids"]] + fixture["rows"])
    return ("\ufeff" + out.getvalue()).encode("utf-8")


def stage_business(data, output_dir):
    workspace = Path(output_dir) / "recommendation"
    product = data["products"][0]
    runtime = DegradedServiceRuntime(None, product["product_code"], product["product_name"])
    store = Store(workspace / "data" / "protocol_demo.db", workspace / "data" / "unused.csv", runtime)
    releases = ProductReleaseService(store, runtime)
    draft = store.create_product_release(product["product_code"], product["product_name"], data)
    validation = releases.validate(draft["release_id"])
    if not validation["valid"]:
        raise ValueError("成品草稿校验失败：%s" % "；".join(validation["errors"]))
    package_path = Path(output_dir) / "business_release.iprelease.json"
    package_path.write_bytes(releases.export_package(draft["release_id"]))
    shutil.copytree(str(ROOT / "app" / "static"), str(workspace / "app" / "static"))
    write_json(workspace / "config" / "model_services.json", {"execution_mode": "services", "local_fallback": False,
               "price_service_url": "http://127.0.0.1:18701", "effectiveness_service_url": "http://127.0.0.1:18702",
               "timeout_seconds": 20, "batch_size": 100})
    return draft, validation


def build(input_path=None, output_dir=None, product_code="VIRTUAL_PIPELINE_PRODUCT", product_name="虚拟执行机构（初版试用）",
          demo=False, effect_config=None, trials=24, folds=3, seeds=(42, 137, 256), target_mape=10.0,
          target_r2=.85, timeout_seconds=180, models=None, log_target=False, search_seed=2026, holdout_seed=20261002,
          missing_tokens=None, virtual_data=False, price_engine="v1", price_config=None, price_overrides=None):
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", str(product_code)):
        raise ValueError("成品代号须以英文字母开头，只包含字母、数字或下划线。")
    if not str(product_name).strip():
        raise ValueError("请填写中文成品名称。")
    output = Path(output_dir or ROOT / "outputs" / ("product_prototype_" + datetime.now().strftime("%Y%m%d_%H%M%S"))).resolve()
    if output == ROOT or ROOT in output.parents and output.parts[len(ROOT.parts)] not in ("outputs", "exports", "deliverables"):
        raise ValueError("项目内构建产物须位于outputs、exports或deliverables下，避免写入运行模型目录。")
    if output.exists() and any(output.iterdir()):
        raise ValueError("输出目录已包含文件，请选用新的目录。")
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    status = {"status": "running", "stage": "input", "product_code": product_code}
    def progress(message, stage=None):
        if stage:
            status["stage"] = stage
        status["message"] = message
        status["elapsed_seconds"] = round(time.monotonic() - started, 2)
        write_json(output / "pipeline_status.json", status)
        print(message, flush=True)
    try:
        from tools.product_price_automl import train_price
        from .product_effectiveness_bootstrap import add_attribute_tags, build_effectiveness, load_config
        if demo:
            fixture = virtual_fixture()
            source = output / "input" / "virtual_products.csv"
            source.parent.mkdir(parents=True, exist_ok=True)
            source.write_bytes(fixture_csv(fixture))
            config = fixture["effectiveness_config"] if not effect_config else load_config(source, effect_config)
            write_json(source.parent / "effectiveness_config.json", config)
        elif input_path:
            source = Path(input_path).resolve()
            config = load_config(source, effect_config,allow_legacy=price_engine not in ('optuna_v2','optuna_v3','optuna_v4','optuna_v5','optuna_v6'))
        else:
            raise ValueError("请提供双行表头CSV/XLSX，或使用--demo。")
        raw = source.read_bytes()
        if price_engine in ('optuna_v2','optuna_v3','optuna_v4','optuna_v5','optuna_v6') and missing_tokens is None:
            from .product_price_optuna_v2 import load_config as load_optuna_config
            if price_engine=='optuna_v3':
                from .product_price_optuna_v3 import load_config as load_optuna_config
            elif price_engine=='optuna_v4':
                from .product_price_optuna_v4 import load_config as load_optuna_config
            elif price_engine=='optuna_v5':
                from .product_price_optuna_v5 import load_config as load_optuna_config
            elif price_engine=='optuna_v6':
                from .product_price_optuna_v6 import load_config as load_optuna_config
            missing_tokens=load_optuna_config(price_config,price_overrides)['input']['missing_tokens']
        table = product_table_rows(source.name, raw, require_dual_header=True)
        write_json(output / "input_diagnostics.json", table["diagnostics"])
        report = HistoricalProductOnboarding().analyze(source.name, raw, product_code, product_name,
                                                     missing_tokens if missing_tokens is not None else ["-1", "\\", "/"],
                                                     require_dual_header=True)
        if not report["price_column_detected"]:
            raise ValueError("成品表须包含中文价格列，建议注明价格(万元)。")
        data = report["data"]
        progress("已读取%d条成品记录、%d个属性，英文代号已保留。" % (report["row_count"], report["attribute_count"]), "price")
        if price_engine in ("optuna_v2","optuna_v3","optuna_v4","optuna_v5","optuna_v6"):
            from .product_price_optuna_v2 import train_price_optuna_v2,load_config as load_optuna_config,workbook_config
            from services.price_service.optuna_v2.typed_features import apply_types,restore_configurations
            if price_engine=='optuna_v3':
                from .product_price_optuna_v3 import load_config as load_optuna_config
            elif price_engine=='optuna_v4':
                from .product_price_optuna_v4 import load_config as load_optuna_config
            elif price_engine=='optuna_v5':
                from .product_price_optuna_v5 import load_config as load_optuna_config
            elif price_engine=='optuna_v6':
                from .product_price_optuna_v6 import load_config as load_optuna_config
            optuna_config=workbook_config(source,load_optuna_config(price_config,price_overrides),{p['parameter_id'] for p in data['parameters']},loader=load_optuna_config)
            restore_configurations(data,table,optuna_config['expert']['field_types'],optuna_config['input']['missing_tokens'])
            apply_types(data,optuna_config['expert']['field_types'])
            if price_engine=='optuna_v6':
                from .product_price_optuna_v6 import train_price_optuna_v6
                price_bundle,price_report=train_price_optuna_v6(data,output/"price",price_config,progress,optuna_config,input_path=source)
            elif price_engine=='optuna_v5':
                from .product_price_optuna_v5 import train_price_optuna_v5
                price_bundle,price_report=train_price_optuna_v5(data,output/"price",price_config,progress,optuna_config,input_path=source)
            elif price_engine=='optuna_v4':
                from .product_price_optuna_v4 import train_price_optuna_v4
                price_bundle,price_report=train_price_optuna_v4(data,output/"price",price_config,progress,optuna_config,input_path=source)
            else:price_bundle,price_report=train_price_optuna_v2(data,output/"price",price_config,progress,optuna_config,input_path=source,engine=price_engine)
        elif price_engine == "r4":
            from tools.product_price_automl_r4 import train_price_r4, load_config as load_r4_config
            from tools.product_r4_input import apply_field_types
            apply_field_types(data, load_r4_config(price_config, price_overrides)["expert"]["field_types"])
            price_bundle, price_report = train_price_r4(data, output / "price", price_config, progress, price_overrides)
        elif price_engine == "v2":
            from tools.product_price_automl_v2 import train_price_v2, workbook_experts
            workbook_overrides = workbook_experts(source, price_config)
            if price_overrides and workbook_overrides:
                raise ValueError("API覆盖配置与工作簿专家覆盖请择一，避免来源不明。")
            price_bundle, price_report = train_price_v2(data, output / "price", price_config, progress, price_overrides or workbook_overrides)
        elif price_engine == "v1":
            price_bundle, price_report = train_price(data, output / "price", trials, folds, seeds, search_seed, holdout_seed,
                                                   target_mape, target_r2, timeout_seconds, models, log_target, progress)
        elif price_engine == "tabpfn":
            from tools.product_price_tabpfn import train_price as train_tabpfn
            price_bundle, price_report = train_tabpfn(data, output / "price", price_config, price_overrides, progress, input_path=source)
        else:
            raise ValueError("未知价格训练引擎：" + str(price_engine))
        progress("价格服务模型已导出，正在构建耦合效能基线。", "effectiveness")
        effect_bundle, effect_report = build_effectiveness(data, output / "effectiveness", config)
        add_attribute_tags(data)
        progress("效能基线已建立，正在创建推荐系统成品草稿。", "recommendation")
        draft, validation = stage_business(data, output)
        # Verify complete, partially missing and enum-containing records through real service backends.
        from services.price_service.app import PriceService
        from services.effectiveness_service.app import EffectivenessService, SnapshotBackend
        price_service = PriceService(output / "price" / "price_native_bundle.pkl")
        effect_service = EffectivenessService(SnapshotBackend(output / "effectiveness" / "effectiveness_bundle.json"))
        checks = []
        for row in data["agreements"][:4]:
            payload = {"product_code": product_code, "parameters": row["params"], "request_id": row["agreement_id"]}
            price_service.handle_post("/api/v1/predict", payload)
            effect_service.handle_post("/api/v1/evaluate", payload)
            checks.append(row["agreement_id"])
        source_copy = output / "input" / source.name
        source_copy.parent.mkdir(parents=True, exist_ok=True)
        if source_copy.resolve() != source.resolve():
            shutil.copy2(str(source), str(source_copy))
        manifest = {"format": "product-prototype-pipeline-1.0", "status": "ready", "prototype": True,
                    "virtual_data": bool(demo or virtual_data), "product_code": product_code, "product_name": product_name,
                    "source_filename": source.name, "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "header_rows": 2, "field_ids": table["field_ids"], "display_language": "zh-CN",
                    "price_model": "price/price_native_bundle.pkl", "effectiveness_model": "effectiveness/effectiveness_bundle.json",
                    "business_package": "business_release.iprelease.json", "workspace": "recommendation",
                    "release_id": draft["release_id"], "release_status": "validated_draft", "training_python": sys.executable,
                    "price_engine": price_engine, "targets_met": price_report["targets_met"], "verified_records": checks,
                    "elapsed_seconds": round(time.monotonic() - started, 2)}
        manifest["artifact_sha256"] = dict((name, hashlib.sha256((output / manifest[name]).read_bytes()).hexdigest())
                                          for name in ("price_model", "effectiveness_model", "business_package"))
        if price_engine in ('optuna_v4','optuna_v5','optuna_v6'):manifest.update(quality_status=price_report['quality_status'],search_mode=price_report['search_mode'])
        write_json(output / "prototype_manifest.json", manifest)
        cv, holdout = price_report["cv_metrics"], price_report["holdout_metrics"]
        if price_engine=='optuna_v6':
            from tools.product_saved_start import fixed_version_launcher_bytes
            text=('# %s：V6独立目标导向流水线\n\n模式：%s；状态：%s。\n\n'
                  '实际部署训练MAPE %.10g%%，R² %s。\n封存验收MAPE %.10g%%，R² %s；范围：%s。\n'
                  '完成外层%d轮；实际fit尝试%d；本次新增%d；缓存命中%d。\n\n'
                  '价格、效能和推荐草稿均已保存；START不训练，EXPORT含模型及环境。\n\n%s\n')%(
                      product_name,price_report['search_mode'],price_report['quality_status'],price_report['train_metrics']['mape_percent'],str(price_report['train_metrics']['r2']),
                      price_report['test_metrics']['mape_percent'],str(price_report['test_metrics']['r2']),cv['scope'],price_report['outer_rounds'],
                      price_report['fit_statistics']['total_attempts'],price_report['model_fits_this_session'],price_report['fit_statistics']['cache_hits_this_session'],price_report['warning'])
            (output/'构建结果.md').write_text(text,encoding='utf-8');(output/'START_PROTOTYPE_WIN7.bat').write_bytes(fixed_version_launcher_bytes())
            status.update(status='completed',stage='complete',targets_met=manifest['targets_met']);progress('V6价格、效能与推荐草稿构建完成：'+str(output));return manifest
        if price_engine=='optuna_v5':
            from tools.product_saved_start import fixed_version_launcher_bytes
            text=('# %s：V5保留基线流水线\n\n模式：%s；状态：%s。\n\n'
                  'V3基线开发MAPE %.4f%%，R² %s。\n当前开发MAPE %.4f%%，R² %s。\n'
                  '当前部署训练MAPE %.4f%%，R² %s。\n累计实际成员fit %d；停止原因：%s。\n\n'
                  'price/selected_snapshot_native.pkl是获分快照；full_refit_native.pkl为全量开发重拟合。\n'
                  'price/price_native_bundle.pkl为当前选择的服务模型；V3参考和复核另报，不混用分数。\n'
                  'MODE_MENU切换；RETRAIN或RESUME继续；START不重训；EXPORT导出保存模型和环境。\n\n%s\n')%(product_name,price_report['search_mode'],price_report['quality_status'],
                    price_report['reference_metrics']['validation']['mape_percent'],str(price_report['reference_metrics']['validation']['r2']),cv['mean_mape'],str(cv['mean_r2']),
                    price_report['deployed_training_metrics']['mape_percent'],str(price_report['deployed_training_metrics']['r2']),price_report['fit_statistics']['total'],price_report['stop_reason'],price_report['warning'])
            (output/'构建结果.md').write_text(text,encoding='utf-8');(output/'START_PROTOTYPE_WIN7.bat').write_bytes(fixed_version_launcher_bytes())
            status.update(status='completed',stage='complete',targets_met=manifest['targets_met']);progress('V5价格、效能与推荐草稿构建完成：'+str(output));return manifest
        if price_engine=='optuna_v4':
            from services.price_service.optuna_v4.optimizer import fmt
            from tools.product_saved_start import fixed_version_launcher_bytes
            text=('# %s：V4流水线构建结果\n\n%d条记录、%d属性；模式%s。\n\n'
                  '当前部署模型训练MAPE %.4f%%，R² %s。\n'
                  '跨划分开发MAPE %.4f%%，汇总R² %s；%d个唯一划分，开发成绩参与选择。\n'
                  '固定外层留出：%s。\n状态：%s。\n\n'
                  '实际配置试验%d轮，最终%d套参数组合；训练、复核、融合和预算详见price/training_report.json。\n'
                  'START只加载保存模型；MODE_MENU切换模式；RETRAIN才训练；EXPORT导出模型与环境。\n\n%s\n')%(product_name,report['row_count'],report['attribute_count'],price_report['search_mode'],
                    price_report['deployed_training_metrics']['mape_percent'],fmt(price_report['deployed_training_metrics']['r2']),cv['mean_mape'],fmt(cv['mean_r2']),
                    price_report['split_plan']['evaluated_splits'],json.dumps(holdout,ensure_ascii=False),price_report['quality_status'],price_report['trials_completed'],price_report['configuration_count'],price_report['warning'])
            (output/'构建结果.md').write_text(text,encoding='utf-8');(output/'START_PROTOTYPE_WIN7.bat').write_bytes(fixed_version_launcher_bytes())
            status.update(status='completed',stage='complete',targets_met=manifest['targets_met']);progress('V4价格、效能和推荐草稿导出完成：'+str(output));return manifest
        if price_engine in ('optuna_v2','optuna_v3'):
            scope='全量重拟合' if price_report['deployment_scope']=='full_data' else '所选划分快照'
            text=('# %s：Optuna V2流水线构建结果\n\n%d条记录、%d个属性。已接通价格、效能与推荐草稿。\n\n'
                  '价格内核：提供版本的六模型NSGA-II联合搜索；划分种子、模型参数和融合权重共同优化。\n'
                  '实际完成%d/%d轮；所选划分种子%d。\n'
                  '所选开发划分MAPE %.4f%%，R² %.6f；不是未经选择的独立测试成绩。\n'
                  '当前服务模型：%s；有效融合成员%d个。目标达到：%s。\n'
                  '全量模型没有独立测试成绩，不套用开发分数。\n\n'
                  'price/price_native_bundle.pkl可供现有价格服务使用；price/price_ensemble_bundle.pkl为原算法全量模型。\n'
                  'price/selected_split_bundle.pkl及selected_split_predictions.csv保留开发划分快照。\n'
                  'Optuna试验、Pareto前沿、所选参数及1—5分专家评分均保存在price目录。\n'
                  '运行START_PROTOTYPE_WIN7.bat直接载入；流水线根目录EXPORT.bat可导出带环境的已训练运行包。\n\n%s\n')%(product_name,report['row_count'],report['attribute_count'],price_report['trials_completed'],price_report['trials_requested'],
                   price_report['representative_seed'],cv['mean_mape'],cv['mean_r2'],scope,price_report['member_count'],manifest['targets_met'],price_report['warning'])
            (output/'构建结果.md').write_text(text,encoding='utf-8')
            if price_engine=='optuna_v3':
                text=text.replace('Optuna V2','Optuna V3').replace('提供版本的六模型NSGA-II联合搜索','依据V3入口设计实现的六模型NSGA-II→多锚点MOTPE联合搜索')
                (output/'构建结果.md').write_text(text,encoding='utf-8')
            from tools.product_saved_start import fixed_version_launcher_bytes
            (output/'START_PROTOTYPE_WIN7.bat').write_bytes(fixed_version_launcher_bytes())
            status.update(status='completed',stage='complete',targets_met=manifest['targets_met']);progress('%s价格、效能与推荐导出完成：%s'%(price_engine,output))
            return manifest
        if price_engine == "tabpfn" and price_report.get("validation_protocol") == "group_loo":
            audit = price_report["nested_cv_metrics"]
            text = "# %s：TabPFN微样本构建结果\n\n%d条记录、%d个原始属性；专家分值用于价格输入属性。\n\n完整嵌套分组留一审核：MAPE %.3f%%，R² %.5f。\n最内层训练至少%d组，外层单条不计算R²，汇总所有折外预测计算。\n独立外部测试：未提供；不能把已反复查看的旧两条记录继续称为独立保留集。\n质量目标达到：%s。\n原生训练成员%d个；数据与评分未改变。\n\n价格bundle和manifest、效能bundle、推荐业务包均已导出。Win11运行价格模型，Win7经HTTP接入。\n运行START_PROTOTYPE_WIN11.bat启动本机试用；Win7使用START_TABPFN_DEMO_WIN7.bat。\n数值仅来自虚拟数据，真实业务精度未认证。\n" % (product_name, report["row_count"], report["attribute_count"], audit["mean_mape"], audit["mean_r2"], price_report["minimum_inner_training_groups"], manifest["targets_met"], price_report["member_count"])
            (output / "构建结果.md").write_text(text, encoding="utf-8")
            launcher = '@echo off\r\nchcp 65001 >nul\r\nsetlocal\r\nset "PYTHONIOENCODING=utf-8"\r\n"%s" "%s" serve --output "%%~dp0." --activate-draft %%*\r\nendlocal\r\n' % (sys.executable, ROOT / "tools" / "product_pipeline.py")
            (output / "START_PROTOTYPE_WIN11.bat").write_bytes(launcher.encode("utf-8"))
            status.update({"status": "completed", "stage": "complete", "targets_met": manifest["targets_met"]})
            progress("微样本价格模型及三环节流水线完成：%s" % output)
            return manifest
        # R4 has a complete nested audit, not an invented two-row external test.
        if price_engine == "r4":
            audit = price_report["nested_cv_metrics"]
            value = lambda x: "未完成/未提供" if x is None else "%.4f" % x
            text = "# %s：R4构建结果\n\n%d条记录、%d个属性。价格评分采用原始1—5分，效能偏好独立。\n\n开发MAPE：%s%%；R²：%s。\n完整外层审核MAPE：%s%%；R²：%s。\n外部测试：未提供。\n质量目标达到：%s。\n\n训练实例%d个，保存配置%d个。\n种子探索记录见price/seed_report.json，各次审核见price/nested_audit.json。\n运行保存模型不会再次训练。\n%s\n" % (product_name,report["row_count"],report["attribute_count"],value(cv["mean_mape"]),value(cv["mean_r2"]),value(audit["mean_mape"]),value(audit["mean_r2"]),manifest["targets_met"],price_report["member_count"],price_report["configuration_count"],price_report["warning"])
            (output / "构建结果.md").write_text(text,encoding="utf-8")
            from tools.product_saved_start import fixed_version_launcher_bytes
            (output / "START_PROTOTYPE_WIN7.bat").write_bytes(fixed_version_launcher_bytes())
            status.update({"status":"completed","stage":"complete","targets_met":manifest["targets_met"]})
            progress("R4初版流水线完成：%s" % output)
            return manifest
        summary = """# %s：初版构建结果

一张双行表头成品表已接通价格预测、耦合效能和推荐草稿。英文代号用于计算，中文名称用于前台显示。

| 项目 | 结果 |
| --- | --- |
| 成品记录 | %d |
| 属性 | %d |
| 价格模型 | %s |
| 训练随机性策略 | %s |
| 交叉验证平均 MAPE | %.3f%% |
| 交叉验证平均 R² | %.4f |
| 独立测试集 MAPE | %.3f%% |
| 独立测试集 R² | %.4f |
| 目标同时达到 | %s |
| 效能耦合关系 | %d |
| 推荐草稿 | 已创建并通过业务结构校验 |

运行本目录的 `START_PROTOTYPE_WIN7.bat` 即可启动独立试用页面并激活本目录的草稿。
价格、效能和草稿JSON可分别接入现有服务及数据中心。

效能分数依赖配置的偏好方向、权重与经验耦合区间，可行性尚未用真实标注校准。
价格最终模型在独立测试评估后使用全部有效记录重训；上表独立测试指标来自重训前的验证模型。
价格区间为开发集交叉验证残差的探索区间，未验证95%%覆盖率。
%s
""" % (product_name, report["row_count"], report["attribute_count"], price_report["selected_model"],
         "配置内种子平均与独立外层审核" if price_engine != "v1" else str(price_report["representative_seed"]), cv["mean_mape"], cv["mean_r2"], holdout["mape_percent"], holdout["r2"],
         "是" if manifest["targets_met"] else "否（保留试用模型，需继续调整或补充数据）", len(effect_report["coupling_models"]),
         "本次输入为虚拟数据，指标仅用于验证软件流程。" if demo or virtual_data else "")
        (output / "构建结果.md").write_text(summary, encoding="utf-8")
        if price_engine == "tabpfn":
            with (output / "构建结果.md").open("a", encoding="utf-8") as handle:
                handle.write("\nTabPFN价格模型须在Win11独立Python3.11环境运行。本机自测使用START_PROTOTYPE_WIN11.bat；Win7验收机使用START_TABPFN_DEMO_WIN7.bat调用Win11价格服务。\n")
        if price_engine != "v1":
            audit = price_report["nested_cv_metrics"]
            with (output / "构建结果.md").open("a", encoding="utf-8") as handle:
                handle.write("\n完整外层审核：MAPE %.3f%%，R² %.4f；原生成员%d个。\n%s\n专家先验与学习结果见price/weight_report.json；可执行设置见price/effective_config.json。\n" %
                             (audit["mean_mape"], audit["mean_r2"], price_report["member_count"], price_report["warning"]))
        launcher = '@echo off\r\nchcp 65001 >nul\r\nsetlocal\r\nset "PYTHONIOENCODING=utf-8"\r\n"%s" "%s" serve --output "%%~dp0." --activate-draft %%*\r\nif errorlevel 1 pause\r\nendlocal\r\n' % (sys.executable, ROOT / "tools" / "product_pipeline.py")
        launcher_name = "START_PROTOTYPE_WIN11.bat" if price_engine == "tabpfn" else "START_PROTOTYPE_WIN7.bat"
        (output / launcher_name).write_bytes(launcher.encode("utf-8"))
        status.update({"status": "completed", "stage": "complete", "targets_met": manifest["targets_met"]})
        progress("初版流水线完成：%s" % output)
        return manifest
    except Exception as exc:
        if getattr(exc, "details", None):
            write_json(output / "input_error_details.json", exc.details)
        status.update({"status": "failed", "error": str(exc)})
        progress("构建失败：%s" % exc)
        raise


def available_port(preferred):
    sock = socket.socket()
    try:
        try:
            sock.bind(("127.0.0.1", int(preferred)))
        except OSError:
            sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def serve(output_dir, activate_draft=False, no_browser=False, price_port=18701, effect_port=18702, app_port=18791, cost_port=18793):
    if os.name == "nt":
        import signal
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    output = Path(output_dir).resolve()
    manifest = json.loads((output / "prototype_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("format") != "product-prototype-pipeline-1.0" or manifest.get("status") != "ready":
        raise ValueError("此目录不是构建完成的初版产物。")
    paths = {}
    for key in ("price_model", "effectiveness_model", "business_package"):
        artifact = (output / manifest[key]).resolve()
        if output not in artifact.parents:
            raise ValueError("初版清单中的文件路径越出输出目录。")
        if hashlib.sha256(artifact.read_bytes()).hexdigest() != manifest["artifact_sha256"][key]:
            raise ValueError("初版产物发生变更，摘要不一致：%s" % key)
        paths[key] = artifact
    ports = [available_port(p) for p in (price_port, effect_port, app_port, cost_port)]
    if len(set(ports)) != 4:
        raise ValueError("价格、效能、推荐系统和效费分析台端口须不同。")
    workspace = output / "recommendation"
    if Path(manifest.get("workspace", "recommendation")) != Path("recommendation"):
        raise ValueError("推荐工作区位置无效。")
    is_tabpfn = manifest.get("price_engine") == "tabpfn"
    if is_tabpfn and sys.version_info < (3, 10):
        raise ValueError("此模型须在Win11 Python3.11运行；Win7请使用run_tabpfn_win7_client.py调用Win11服务。")
    config = {"execution_mode": "services", "local_fallback": False,
              "price_service_url": "http://127.0.0.1:%d" % ports[0],
              "effectiveness_service_url": "http://127.0.0.1:%d" % ports[1], "timeout_seconds": 180 if is_tabpfn else 20, "batch_size": 64 if is_tabpfn else 100,
              "price_output": {"unit":"wan_yuan","scale":1.0}}
    write_json(workspace / "config" / "model_services.json", config)
    analysis_url="http://127.0.0.1:%d/" % ports[3]
    portal_path=workspace/'config/service_portal.json'
    portal=json.loads(portal_path.read_text(encoding='utf-8')) if portal_path.is_file() else {}
    service_links=portal.setdefault('services',{})
    for key,label,url in [('recommendation','智能推荐','/'),('quick_price','简易价格预测','/price'),
                          ('effectiveness','效能评价','/effectiveness'),('admin','数据管理','/admin'),
                          ('cost_effectiveness_analysis','效费分析台',analysis_url)]:
        service_links.setdefault(key,{}).update(label=label,url=url,enabled=True,visible=True)
    write_json(portal_path,portal)
    from .process_group import OwnedServices
    managed=OwnedServices();children,handles,server=managed.children,[],None
    old_term=None
    if threading.current_thread() is threading.main_thread():
        import signal
        old_term=signal.signal(signal.SIGTERM,signal.default_int_handler)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        child_env = dict(os.environ)
        child_env.update({"PYTHONIOENCODING": "utf-8", "PRICE_ALLOW_DEGRADED": "0", "PRICE_ALLOW_MODEL_FALLBACK": "0",
                          "EFFECT_SOURCE_ROOT": "", "EFFECT_WORKBOOK": "", "EFFECT_RUNTIME_PACKAGE": ""})
        child_env.update(COST_EFFECTIVENESS_DATABASE=str(workspace/'data/protocol_demo.db'),
                         COST_EFFECTIVENESS_RECOMMENDATION_URL='http://127.0.0.1:%d/portal'%ports[2],
                         COST_EFFECTIVENESS_PRICE_URL=config['price_service_url'],
                         COST_EFFECTIVENESS_EFFECTIVENESS_URL=config['effectiveness_service_url'],
                         COST_EFFECTIVENESS_PRICE_OUTPUT_UNIT='wan_yuan',COST_EFFECTIVENESS_PRICE_OUTPUT_SCALE='1')
        price_module = "services.tabpfn_price_service.app" if is_tabpfn else "services.price_service.app"
        effect_args=['--package',str(paths['effectiveness_model']),'--snapshot',''] if manifest.get('effectiveness_backend')=='expert_package' else ['--package','','--snapshot',str(paths['effectiveness_model'])]
        commands = [[sys.executable, "-m", price_module, "--model", str(paths["price_model"]), "--port", str(ports[0])],
                    [sys.executable, "-m", "services.effectiveness_service.app"]+effect_args+["--port",str(ports[1])],
                    [sys.executable,"-m","cost_effectiveness_analysis.app","--host","127.0.0.1","--port",str(ports[3])]]
        for i, command in enumerate(commands):
            command=command[:1]+[str(ROOT/'automl_pipeline/service_runner.py')]+command[2:]
            handle = (output / ("price_service.log","effectiveness_service.log","cost_effectiveness_service.log")[i]).open("ab")
            handles.append(handle)
            managed.spawn(command,cwd=str(ROOT),env=child_env,stdout=handle,stderr=handle,
                          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        deadline = time.monotonic() + 45
        for i, url in enumerate((config["price_service_url"], config["effectiveness_service_url"],analysis_url.rstrip('/'))):
            while True:
                if children[i].poll() is not None:
                    raise RuntimeError("模型服务启动失败，详情见输出目录的服务日志。")
                try:
                    with opener.open(url + "/health", timeout=2) as response:
                        health = json.loads(response.read().decode("utf-8"))
                    if (i < 2 and health.get("product_code") == manifest["product_code"]) or (i == 2 and health.get('service')=='cost-effectiveness-analysis' and health.get('status')=='ok'):
                        break
                except Exception:
                    pass
                if time.monotonic() >= deadline:
                    raise RuntimeError("等待模型服务超时。")
                time.sleep(.15)
        # These overrides are local to this launcher and prevent old project env settings from redirecting services.
        os.environ.update({"IPDEMO_MODEL_EXECUTION_MODE": "services", "IPDEMO_PRICE_SERVICE_URL": config["price_service_url"],
                           "IPDEMO_EFFECT_SERVICE_URL": config["effectiveness_service_url"], "IPDEMO_MODEL_SERVICE_FALLBACK": "0",
                           "IPDEMO_PRICE_OUTPUT_UNIT":"wan_yuan","IPDEMO_PRICE_OUTPUT_SCALE":"1",
                           "IPDEMO_DEMO_READ_ONLY": "0",'IPDEMO_EFFECT_INTEGRATION_KIND':manifest.get('effectiveness_backend','trial_snapshot')})
        staging_runtime = DegradedServiceRuntime(None, manifest["product_code"], manifest["product_name"])
        staging_store = Store(workspace / "data" / "protocol_demo.db", workspace / "data" / "unused.csv", staging_runtime)
        releases = ProductReleaseService(staging_store, staging_runtime)
        if activate_draft and releases.get(manifest["release_id"])["status"] != "active":
            releases.activate(manifest["release_id"])
        elif staging_store.is_empty():
            raise ValueError("首次试运行请加--activate-draft，系统只切换输出目录中的业务数据。")
        from app.server import create_server
        server = create_server(workspace, "127.0.0.1", ports[2])
        # Saved models carry historical static copies. Always serve the current
        # verified UI; this upgrade must also apply when selecting an old model.
        server.RequestHandlerClass.app.static_dir=ROOT/'app/static'
        url = "http://127.0.0.1:%d/" % ports[2]
        portal_url=url+'portal'
        write_json(output / "running.json", {"pid": os.getpid(), "url": url, "price_url": config["price_service_url"],
                                            "effectiveness_url": config["effectiveness_service_url"], "workspace": str(workspace),
                                            'portal_url':portal_url,'cost_effectiveness_url':analysis_url,
                                            'child_pids':[p.pid for p in children],'process_group_managed':os.name=='nt'})
        print("统一门户：%s\n推荐系统（V21.6）：%s\n效费分析台：%s\n数据中心：%sadmin\n关闭启动窗口或按Ctrl+C会一并停止四个服务。" % (portal_url,url,analysis_url,url), flush=True)
        if not no_browser:
            webbrowser.open(portal_url)
        server.serve_forever(poll_interval=.25)
    except KeyboardInterrupt:
        print("正在停止初版服务。", flush=True)
    finally:
        if server:
            server.server_close()
        managed.close()
        if old_term is not None:signal.signal(signal.SIGTERM,old_term)
        for handle in handles:
            handle.close()
        if (output / "running.json").exists():
            (output / "running.json").unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description="成品表到三环节初版模型的一键流水线")
    sub = parser.add_subparsers(dest="command")
    builder = sub.add_parser("build", help="构建价格模型、效能基线与推荐草稿")
    builder.add_argument("input", nargs="?", help="第一行中文名、第二行英文代号的CSV/XLSX")
    builder.add_argument("--demo", action="store_true", help="用内置确定性虚拟数据验证")
    builder.add_argument("--virtual-data", action="store_true", help="将输入文件明确标为虚拟验证数据")
    builder.add_argument("--output")
    builder.add_argument("--product-code", default="VIRTUAL_PIPELINE_PRODUCT")
    builder.add_argument("--product-name", default="虚拟执行机构（初版试用）")
    builder.add_argument("--effect-config", help="可选效能偏好与耦合JSON；XLSX也可带属性配置、耦合关系表")
    builder.add_argument("--price-engine", choices=["v1", "v2", "r4", "tabpfn", "optuna_v2", "optuna_v3", "optuna_v4", "optuna_v5", "optuna_v6"], default="v1", help="V2/V3/V4/V5/V6可切换价格搜索引擎")
    builder.add_argument("--price-config", help="与所选训练引擎匹配的JSON；旧版搜索参数仅用于V1")
    builder.add_argument("--trials", type=int, default=24)
    builder.add_argument("--folds", type=int, default=3)
    builder.add_argument("--seeds", default="42,137,256")
    builder.add_argument("--search-seed", type=int, default=2026)
    builder.add_argument("--holdout-seed", type=int, default=20261002)
    builder.add_argument("--target-mape", type=float, default=10.0, help="百分数，例如10表示10%%")
    builder.add_argument("--target-r2", type=float, default=.85)
    builder.add_argument("--timeout", type=float, default=180, help="搜索秒数预算；当前试验结束后检查预算")
    builder.add_argument("--models", default="", help="可选逗号分隔候选模型，默认8种")
    builder.add_argument("--log-target", action="store_true")
    builder.add_argument("--missing", default="-1,\\,/")
    launcher = sub.add_parser("serve", help="启动构建结果的独立三服务试用目录")
    launcher.add_argument("--output", required=True)
    launcher.add_argument("--activate-draft", action="store_true", help="只激活输出目录内的草稿")
    launcher.add_argument("--no-browser", action="store_true")
    launcher.add_argument("--price-port", type=int, default=18701)
    launcher.add_argument("--effect-port", type=int, default=18702)
    launcher.add_argument("--app-port", type=int, default=18791)
    launcher.add_argument("--cost-port", type=int, default=18793)
    fixture = sub.add_parser("fixture", help="生成示例工作簿作者工具使用的虚拟数据JSON")
    fixture.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    if args.command == "build":
        if args.demo and args.input:
            parser.error("--demo与输入文件只能选择一个")
        if args.price_config and args.price_engine == "v1":
            parser.error("--price-config只用于V2和TabPFN")
        if args.price_engine != "v1":
            legacy = {"--trials", "--folds", "--seeds", "--search-seed", "--holdout-seed", "--target-mape", "--target-r2", "--timeout", "--models", "--log-target"}
            if any(token.split("=")[0] in legacy for token in (argv if argv is not None else sys.argv[1:])):
                parser.error("训练设置请统一写入--price-config，旧版调参标志仅用于V1。")
        manifest = build(args.input, args.output, args.product_code, args.product_name, args.demo, args.effect_config,
                         args.trials, args.folds, [int(s.strip()) for s in args.seeds.split(",") if s.strip()],
                         args.target_mape, args.target_r2, args.timeout,
                         [m.strip() for m in args.models.split(",") if m.strip()] or None, args.log_target,
                         args.search_seed, args.holdout_seed, [x.strip() for x in args.missing.split(",") if x.strip()], args.virtual_data,
                         args.price_engine, args.price_config)
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
    elif args.command == "serve":
        serve(args.output, args.activate_draft, args.no_browser, args.price_port, args.effect_port, args.app_port,args.cost_port)
    elif args.command == "fixture":
        write_json(args.output, virtual_fixture())
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
