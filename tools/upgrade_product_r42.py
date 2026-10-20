# -*- coding: utf-8 -*-
"""Apply an explicit training profile; never retrain or change saved artifacts."""
import argparse
import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.product_price_automl_r4 import load_config
from tools.product_r42_profiles import profile


PATCH_FILES=("tools/product_price_automl_r4.py","tools/product_r42_search.py","tools/product_r42_profiles.py","tools/product_r4_input.py", "tools/upgrade_product_r42.py",
             "services/price_service/r4_estimators.py","services/price_service/app.py","app/static/price_ensemble.js",
             "APPLY_R4_2_DATA_ONLY.bat","APPLY_R4_2_QUICK.bat","APPLY_R4_2_EXPERT_COMPARE.bat","APPLY_R4_2_LOO_AUDIT.bat")


def upgrade(target,name="data_only"):
    target=Path(target).resolve();path=target/"config/price_automl_r4.json"
    if not path.is_file() or not (target/"config/portable.json").is_file():raise ValueError("请将R4.2补丁解压到原R4/R4.1目录。")
    old=load_config(path);new=profile(name)
    # Preserve the business thresholds, original ratings and typed input schema.
    new["quality"]=copy.deepcopy(old["quality"]);new["missing_tokens"]=copy.deepcopy(old["missing_tokens"])
    enabled=new["expert"]["enabled"];new["expert"]=copy.deepcopy(old["expert"]);new["expert"]["enabled"]=enabled
    new["resources"]["cache_directory"]=old["resources"]["cache_directory"]
    load_config(overrides=new)
    stamp=datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    path.with_name("price_automl_r4.before_r42_"+stamp+".json").write_bytes(path.read_bytes())
    pending=path.with_suffix(".json.tmp");pending.write_text(json.dumps(new,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");pending.replace(path)
    manifest_path=target/"PACKAGE_MANIFEST.json"
    if manifest_path.is_file():
        (target/"config"/("PACKAGE_MANIFEST.before_r42_"+stamp+".json")).write_bytes(manifest_path.read_bytes())
        manifest=json.loads(manifest_path.read_text(encoding="utf-8"));entries={r["path"]:r for r in manifest["immutable_files"]}
        for rel in PATCH_FILES:
            file=target/rel
            if file.is_file():entries[rel]={"path":rel,"size":file.stat().st_size,"sha256":hashlib.sha256(file.read_bytes()).hexdigest()}
        manifest["immutable_files"]=sorted(entries.values(),key=lambda r:r["path"]);manifest["training_revision"]="R4_2_flexible_cv_multi_model"
        pending=manifest_path.with_suffix(".json.tmp");pending.write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8");pending.replace(manifest_path)
    print("已启用R4.2配置：%s。专家价格评分%s，外层%s，重复%d次。"%(name,"参与训练" if enabled else "仅作对照",new["validation"]["outer_strategy"],new["validation"]["outer_repeats"]),flush=True)
    print("原精度门槛、评分及数据保留；运行RETRAIN.bat训练。START.bat继续加载现有模型。",flush=True)
    return new


def main():
    p=argparse.ArgumentParser();p.add_argument("--target",default=str(ROOT));p.add_argument("--profile",default="data_only",choices=["data_only","expert_compare","quick","loo_audit"]);a=p.parse_args()
    try:upgrade(a.target,a.profile)
    except Exception as exc:print("R4.2配置失败："+str(exc),flush=True);return 1
    return 0
if __name__=="__main__":sys.exit(main())
