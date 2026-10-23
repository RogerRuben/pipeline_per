# -*- coding: utf-8 -*-
"""Relocatable, offline entry point: replace the input table and run START.bat."""
from __future__ import print_function

import argparse
import contextlib
import hashlib
import importlib
import json
import multiprocessing as mp
import os
import struct
import sys
import traceback
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.console_compat import enable_plain_console
enable_plain_console()
EXPECTED = {"numpy": "1.19.5", "scipy": "1.5.4", "pandas": "1.3.5", "sklearn": "0.24.2",
            "optuna": "3.6.0", "joblib": "1.0.1", "threadpoolctl": "2.1.0"}


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    os.replace(str(temporary), str(path))


def inside(path, directory):
    path, directory = Path(path).resolve(), Path(directory).resolve()
    return path == directory or directory in path.parents


def verify_runtime(full=False):
    errors, versions = [], {}
    if sys.version_info[:3] != (3, 8, 10) or struct.calcsize("P") != 8:
        errors.append("请通过包内BAT使用自带Python3.8.10 x64。")
    if not inside(sys.executable, ROOT / "runtime" / "python38"):
        errors.append("当前解释器不属于交付包。")
    for name, expected in EXPECTED.items():
        try:
            module = importlib.import_module(name)
            versions[name] = str(module.__version__)
            if versions[name] != expected or not inside(module.__file__, ROOT / "runtime"):
                errors.append("依赖版本或来源不匹配：" + name)
        except Exception as exc:
            errors.append("依赖无法加载：%s：%s" % (name, exc))
    if not errors:
        import numpy as np
        from scipy.optimize import minimize
        from sklearn.ensemble import RandomForestRegressor
        import sqlite3
        result = minimize(lambda x: float((x[0] - 2) ** 2), [0.0], method="SLSQP")
        if not result.success or abs(result.x[0] - 2) > 1e-4:
            errors.append("数值优化自检失败。")
        RandomForestRegressor(n_estimators=3, random_state=1, n_jobs=1).fit(np.arange(10).reshape(-1, 1), np.arange(10))
        with sqlite3.connect(":memory:") as connection:
            connection.execute("select 1").fetchone()
    verified = 0
    if full:
        manifest = json.loads((ROOT / "PACKAGE_MANIFEST.json").read_text(encoding="utf-8"))
        for item in manifest["immutable_files"]:
            path = (ROOT / item["path"]).resolve()
            if not inside(path, ROOT) or not path.is_file() or digest(path) != item["sha256"]:
                errors.append("包内文件校验失败：" + item["path"])
            verified += 1
    report = {"status": "FAIL" if errors else "PASS", "python": sys.version.split()[0], "bits": struct.calcsize("P") * 8,
              "dependency_versions": versions, "immutable_files_verified": verified,
              "root": str(ROOT), "external_python_required": False, "internet_required": False,
              "console_mode": "plain_device_safe", "errors": errors}
    write_json(ROOT / "logs" / "runtime_verification.json", report)
    if errors:
        raise ValueError("；".join(errors))
    return report


def resolve_input(value=None):
    if value:
        path = Path(value).resolve()
    else:
        choices = [ROOT / "input" / name for name in ("products.xlsx", "products.csv") if (ROOT / "input" / name).is_file()]
        if len(choices) != 1:
            raise ValueError("input目录须仅有一份products.xlsx或products.csv，也可将表格拖到START.bat。")
        path = choices[0]
    if not path.is_file() or path.suffix.lower() not in (".xlsx", ".csv"):
        raise ValueError("请提供存在的双行表头.xlsx或.csv成品表。")
    return path


def prepare(source,price_engine=None):
    """Do not carry a partial engineering equation across a changed schema."""
    from app.historical_onboarding import HistoricalProductOnboarding
    from app.product_table import product_table_rows, clean_field_id
    from app.xlsx_utils import read_workbook_bytes
    from tools.product_price_automl import price_dataset
    from tools.product_price_automl_v2 import load_config, FAMILIES
    from tools.product_effectiveness_bootstrap import load_config as effect_config
    settings_path = ROOT / "config" / "portable.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8-sig"))
    if price_engine is not None:settings['price_engine']=price_engine
    if settings.get("price_engine") == "r4":
        from tools.product_r4_input import prepare as prepare_r4
        return prepare_r4(source, ROOT, settings)
    if settings.get("price_engine") in ("optuna_v2","optuna_v3","optuna_v4","optuna_v5"):
        from tools.product_optuna_v2_input import prepare as prepare_optuna_v2
        return prepare_optuna_v2(source, ROOT, settings)
    config_path = ROOT / "config" / "price_automl_v2.json"
    price = load_config(config_path)
    raw = source.read_bytes()
    write_json(ROOT / "logs" / "input_diagnostics.json", {"status": "READING", "filename": source.name})
    table = product_table_rows(source.name, raw, require_dual_header=True)
    write_json(ROOT / "logs" / "input_diagnostics.json", table["diagnostics"])
    analyzed = HistoricalProductOnboarding().analyze(source.name, raw, settings["product_code"], settings["product_name"],
                                                    settings["missing_tokens"], require_dual_header=True)
    if not analyzed["price_column_detected"]:
        raise ValueError("成品表须包含价格(万元)列。")
    frame, y, specs, mappings, groups, ids = price_dataset(analyzed["data"], 10)
    if len(set(float(value) for value in y)) < 2:
        raise ValueError("训练价格须有变化，不能全部相同。")
    numeric = {p["parameter_id"] for p in specs if p["value_type"] != "enum"}
    notes = []
    notes.append("成品数据读取工作表：%s。" % table["sheet_name"])
    if set(price["small_sample"]["expert_features"]) - set(frame.columns):
        price["small_sample"]["expert_features"] = []
        notes.append("原专家价格特征子集与新字段不匹配，停用该子集并使用通用稳健模型。")
    sheets = read_workbook_bytes(raw) if source.suffix.lower() == ".xlsx" else {}
    if settings["read_expert_sheets"]:
        if "专家模型权重" in sheets:
            price["ensemble"]["family_priors"] = [
                {"family": clean_field_id(r[0]), "label": str(r[1] or r[0]), "weight": float(r[2]), "confidence": float(r[3]),
                 "lower_bound": float(r[4] or 0) if len(r) > 4 else 0., "upper_bound": float(r[5]) if len(r) > 5 else 1.}
                for r in sheets["专家模型权重"][1:] if r and str(r[0] or "").strip()]
            notes.append("价格模型族先验来自本次工作簿。")
        if "价格工程先验" in sheets:
            engineering = {"intercept_wan": 0., "coefficients": {}}
            for row in sheets["价格工程先验"][1:]:
                if row and str(row[0] or "").strip():
                    key = clean_field_id(row[0])
                    if key == "__intercept__":
                        engineering["intercept_wan"] = float(row[2])
                    else:
                        if key in engineering["coefficients"]:
                            raise ValueError("工程成本属性重复：" + key)
                        engineering["coefficients"][key] = float(row[2])
            price["engineering"] = engineering
            notes.append("工程成本公式来自本次工作簿。")
    coefficients = price["engineering"]["coefficients"]
    incompatible = sorted(set(coefficients) - numeric)
    if incompatible or not coefficients:
        price["engineering"] = {"intercept_wan": 0., "coefficients": {}}
        price["models"] = [name for name in price["models"] if not name.startswith("engineering")]
        notes.append("工程模型未启用：" + ("成本公式包含不存在或非数值属性：" + ",".join(incompatible) if incompatible else "未提供工程成本公式。"))
    else:
        for name in ("engineering_cost", "engineering_residual"):
            if name not in price["models"]:
                price["models"].insert(0, name)
    available = {FAMILIES[name] for name in price["models"]}
    retained = []
    for row in price["ensemble"]["family_priors"]:
        if row["family"] in available:
            retained.append(row)
        else:
            if float(row.get("lower_bound", 0)) > 0:
                raise ValueError("专家硬性最低份额对应的模型族不可用：" + row["family"])
            notes.append("未使用不可用模型族的软先验：" + row["family"])
    if len(retained) != len(price["ensemble"]["family_priors"]):
        total = sum(float(row["weight"]) for row in retained)
        if total > 0:
            for row in retained:
                row["weight"] = float(row["weight"]) / total
            notes.append("可用模型族的先验按原比例归一化。")
    price["ensemble"]["family_priors"] = retained
    if not retained and price["ensemble"]["mode"] in ("performance_budget", "soft_prior"):
        raise ValueError("当前模型族没有可用专家先验，请检查专家配置。")
    pairs = price["pipeline"]["interaction_pairs"]
    price["pipeline"]["interaction_pairs"] = [pair for pair in pairs if len(pair) == 2 and set(pair) <= numeric]
    if len(pairs) != len(price["pipeline"]["interaction_pairs"]):
        notes.append("已排除引用未知或枚举属性的交互特征。")
    price["experts_from_workbook"] = False  # This adapter has resolved the single source of truth.
    price["resources"]["cache_directory"] = str(ROOT / "outputs" / "oof_cache")
    load_config(overrides=price)
    effect = effect_config(source)
    attributes = effect["attributes"]
    keys = set(frame.columns)
    dropped = sorted(set(attributes) - keys)
    effect["attributes"] = {key: value for key, value in attributes.items() if key in keys}
    before = len(effect["couplings"])
    effect["couplings"] = [edge for edge in effect["couplings"] if edge.get("source") in numeric and edge.get("target") in numeric]
    if dropped or before != len(effect["couplings"]):
        notes.append("效能配置适配：排除%d个未知属性、%d条无对应数值属性的耦合。" % (len(dropped), before - len(effect["couplings"])))
    notes.append("效能方向、权重和经验可行性仍为初版假设，具体设置保存在效能报告。")
    code_files = ("product_portable.py", "product_pipeline.py", "product_price_automl_v2.py", "product_price_micro.py", "product_effectiveness_bootstrap.py", "price_expert_fusion_probe.py")
    identity = {"input_sha256": hashlib.sha256(raw).hexdigest(), "settings": settings, "price_config": json.loads(config_path.read_text(encoding="utf-8-sig")),
                "code": {name: digest(ROOT / "tools" / name) for name in code_files}}
    identity["code"]["ensemble_estimators.py"] = digest(ROOT / "services/price_service/ensemble_estimators.py")
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    details = {"records": len(frame), "attributes": len(specs), "independent_groups": len(set(groups.tolist())),
               "input_sha256": identity["input_sha256"], "source_filename": source.name, "notes": notes,
               "input_diagnostics": table["diagnostics"],
               "engineering_enabled": bool(price["engineering"]["coefficients"]), "fingerprint": fingerprint,
               "virtual_data": identity["input_sha256"] == settings["demo_input_sha256"]}
    return settings, price, effect, details


def latest_output(fingerprint=None):
    path = ROOT / "state" / "latest.json"
    expected_engine=None
    settings_path=ROOT/'config/portable.json'
    if settings_path.is_file():
        settings=json.loads(settings_path.read_text(encoding='utf-8-sig'))
        if settings.get('mode_registry_enabled'):
            from tools.product_modes import selected_pointer
            entry=selected_pointer(ROOT)
            if entry is None or (fingerprint is not None and entry.get('fingerprint')!=fingerprint):return None
            return (ROOT/entry['output']).resolve()
        if settings.get('engine_selection_required') and settings.get('price_engine') in ('optuna_v2','optuna_v3','optuna_v4','optuna_v5'):
            expected_engine=settings['price_engine']
            path=ROOT/'state'/('latest_'+settings['price_engine']+'.json')
    if not path.is_file():
        return None
    latest = json.loads(path.read_text(encoding="utf-8"))
    output = (ROOT / latest["output"]).resolve()
    if not inside(output, ROOT / "outputs"):
        raise ValueError("最新构建位置越出包内outputs目录。")
    if fingerprint is not None and latest.get("fingerprint") != fingerprint:
        return None
    manifest_path = output / "prototype_manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if expected_engine is not None and manifest.get('price_engine')!=expected_engine:return None
    for key in ("price_model", "effectiveness_model", "business_package"):
        artifact = (output / manifest[key]).resolve()
        if not inside(artifact, output) or not artifact.is_file() or digest(artifact) != manifest["artifact_sha256"][key]:
            return None
    return output


class Tee(object):
    _product_safe_console_stream = True

    def __init__(self, *streams):
        self.streams = streams
    def write(self, value):
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)
    def flush(self):
        for stream in self.streams:
            stream.flush()

    def isatty(self):
        return False

    def fileno(self):
        return -1


def rebuild(source, prepared):
    from tools.product_pipeline import build
    settings, price, effect, details = prepared
    token = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    output = ROOT / "outputs" / ("run_" + token)
    config_dir = ROOT / "state" / ("build_" + token)
    price_path, effect_path = config_dir / "price.json", config_dir / "effectiveness.json"
    write_json(price_path, price)
    write_json(effect_path, effect)
    (ROOT / "logs").mkdir(exist_ok=True)
    print("本次输入：%d条有效记录，%d个属性。" % (details["records"], details["attributes"]), flush=True)
    for note in details["notes"]:
        print(note, flush=True)
    log_path = ROOT / "logs" / ("build_" + token + ".log")
    with log_path.open("w", encoding="utf-8") as handle, contextlib.redirect_stdout(Tee(sys.stdout, handle)), contextlib.redirect_stderr(Tee(sys.stderr, handle)):
        build(source, output, settings["product_code"], settings["product_name"], effect_config=effect_path,
              virtual_data=details["virtual_data"], price_engine=settings.get("price_engine", "v2"), price_config=price_path,
              missing_tokens=price.get("input",{}).get("missing_tokens",price.get("missing_tokens", settings["missing_tokens"])))
    write_json(output / "input_adaptation.json", details)
    manifest_path = output / "prototype_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["training_python"] = "runtime/python38/python.exe"
    manifest["portable_entrypoint"] = "START.bat"
    write_json(manifest_path, manifest)
    from tools.product_saved_start import fixed_version_launcher_bytes
    (output / "START_PROTOTYPE_WIN7.bat").write_bytes(fixed_version_launcher_bytes())
    pointer={"format": "product-portable-latest-1.0", "status": "ready",
                                               "output": output.relative_to(ROOT).as_posix(), "fingerprint": details["fingerprint"],
                                               "input_sha256": details["input_sha256"],"price_engine":settings.get('price_engine')}
    write_json(ROOT/'state/latest.json',pointer)
    if settings.get('price_engine') in ('optuna_v2','optuna_v3','optuna_v4','optuna_v5'):write_json(ROOT/'state'/('latest_'+settings['price_engine']+'.json'),pointer)
    if settings.get('mode_registry_enabled'):
        from tools.product_modes import register
        register(ROOT,pointer,price)
    print("构建完成：%s。启动页面请运行START.bat。" % output.relative_to(ROOT), flush=True)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description="离线成品流水线：更换input/products.xlsx后运行START.bat")
    parser.add_argument("command", choices=("start", "build", "verify", "inspect"))
    parser.add_argument("input", nargs="?")
    parser.add_argument("--output", help="启动指定的包内构建结果")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--full", action="store_true", help="校验全部不可变运行文件摘要")
    parser.add_argument("--price-port", type=int)
    parser.add_argument("--effect-port", type=int)
    parser.add_argument("--app-port", type=int)
    args = parser.parse_args(argv)
    try:
        report = verify_runtime(args.full)
        if args.command == "verify":
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0
        if args.output:
            if args.command != "start":
                raise ValueError("--output仅用于启动已构建结果。")
            output = Path(args.output).resolve()
            if not inside(output, ROOT / "outputs"):
                raise ValueError("只能启动包内outputs下的构建结果。")
            settings = json.loads((ROOT / "config" / "portable.json").read_text(encoding="utf-8"))
        elif args.command == "start" and json.loads((ROOT / "config" / "portable.json").read_text(encoding="utf-8")).get("startup_policy") == "saved_only":
            settings = json.loads((ROOT / "config" / "portable.json").read_text(encoding="utf-8"))
            output = latest_output()
            if output is None:
                raise ValueError("没有完整保存版本；请先运行TRAIN.bat。START.bat不会自动训练。")
            print("直接载入已保存版本，不读取训练表或重新训练。", flush=True)
        else:
            source = resolve_input(args.input)
            prepared = prepare(source)
            settings, price, effect, details = prepared
            if args.command == "inspect":
                print(json.dumps(details, ensure_ascii=False, indent=2))
                return 0
            output = None if args.command == "build" else latest_output(details["fingerprint"])
            if output is None:
                print("检测到新数据/配置，开始构建价格、效能与推荐。", flush=True)
                output = rebuild(source, prepared)
            else:
                print("数据和配置未变化，使用已验证构建：%s" % output.relative_to(ROOT), flush=True)
        if args.command == "start":
            from tools.product_pipeline import serve
            os.environ["IPDEMO_AUTH_ENABLED"] = "0"
            serve(output, activate_draft=True, no_browser=args.no_browser,
                  price_port=args.price_port if args.price_port is not None else settings["ports"]["price"],
                  effect_port=args.effect_port if args.effect_port is not None else settings["ports"]["effectiveness"],
                  app_port=args.app_port if args.app_port is not None else settings["ports"]["recommendation"])
        return 0
    except KeyboardInterrupt:
        print("任务已停止。")
        return 130
    except Exception as exc:
        (ROOT / "logs").mkdir(exist_ok=True)
        diagnostic_path = ROOT / "logs" / "input_diagnostics.json"
        details = getattr(exc, "details", None)
        if details is not None:
            write_json(diagnostic_path, dict(details, status="FAIL"))
        elif diagnostic_path.is_file():
            details = json.loads(diagnostic_path.read_text(encoding="utf-8"))
            if details.get("status") == "READING":
                write_json(diagnostic_path, dict(details, status="FAIL", error=str(exc)))
        with (ROOT / "logs" / "last_error.log").open("w", encoding="utf-8") as handle:
            traceback.print_exc(file=handle)
        print("操作失败：%s\n详情见logs/last_error.log；表头诊断见logs/input_diagnostics.json；已有构建保留。" % exc, file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    mp.freeze_support()
    sys.exit(main())
