# -*- coding: utf-8 -*-
"""Merge R4.1 defaults while preserving user expert scores and quality targets."""
import argparse
import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from tools.product_price_automl_r4 import load_config,DEFAULT


def upgrade(target):
    target=Path(target).resolve(); path=target/"config/price_automl_r4.json"
    if not path.is_file() or not (target/"config/portable.json").is_file():
        raise ValueError("请将修复包解压到原R4目录（能看到START.bat与config），再执行本脚本。")
    raw=json.loads(path.read_text(encoding="utf-8-sig")); settings=load_config(overrides=raw)
    if raw.get("search",{}).get("ridge_alphas")==[.01,.1,1.,10.]: settings["search"]["ridge_alphas"]=copy.deepcopy(DEFAULT["search"]["ridge_alphas"])
    if raw.get("pipeline",{}).get("feature_views")==["all","expert_core"]: settings["pipeline"]["feature_views"]=copy.deepcopy(DEFAULT["pipeline"]["feature_views"])
    legacy=[m for m in DEFAULT["models"] if m!="relative_ridge"]
    if raw.get("models")==legacy: settings["models"]=copy.deepcopy(DEFAULT["models"])
    # Preserve explicit settings; new fields are supplied by the merged defaults.
    load_config(overrides=settings)
    if settings["expert"]!=load_config(overrides=raw)["expert"] or settings["quality"]!=load_config(overrides=raw)["quality"]:
        raise AssertionError("升级不得改变专家输入与业务精度门槛。")
    stamp=datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup=path.with_name("price_automl_r4.before_r41_"+stamp+".json"); backup.write_bytes(path.read_bytes())
    pending=path.with_suffix(".json.tmp"); pending.write_text(json.dumps(settings,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); pending.replace(path)
    package_manifest=target/"PACKAGE_MANIFEST.json"
    if package_manifest.is_file():
        manifest=json.loads(package_manifest.read_text(encoding="utf-8"))
        (config_directory:=target/"config").joinpath("PACKAGE_MANIFEST.before_r41_"+stamp+".json").write_bytes(package_manifest.read_bytes())
        entries={item["path"]:item for item in manifest["immutable_files"]}
        # Update only this patch's files; all unmodified runtime checks remain
        # intact. Saved models keep their separate artifact checksums.
        for relative in ("tools/product_price_automl_r4.py","services/price_service/r4_estimators.py","tools/upgrade_product_r41.py","APPLY_R4_1_FIX.bat"):
            file=target/relative
            if file.is_file(): entries[relative]={"path":relative,"size":file.stat().st_size,"sha256":hashlib.sha256(file.read_bytes()).hexdigest()}
        manifest["immutable_files"]=sorted(entries.values(),key=lambda item:item["path"])
        manifest["training_revision"]="R4_1_audit_and_performance_fix"
        temporary=package_manifest.with_suffix(".json.tmp"); temporary.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8"); temporary.replace(package_manifest)
    print("R4.1训练设置已合并；专家评分、目标、数据及已保存模型保留。",flush=True)
    print("配置备份："+str(backup),flush=True)
    print("先结束仍在运行的旧训练，再运行RETRAIN.bat；START.bat仍只加载保存模型。",flush=True)
    return backup


def main():
    p=argparse.ArgumentParser(); p.add_argument("--target",default=str(ROOT)); a=p.parse_args()
    try: upgrade(a.target)
    except Exception as exc: print("升级失败："+str(exc),flush=True); return 1
    return 0
if __name__=="__main__": sys.exit(main())
