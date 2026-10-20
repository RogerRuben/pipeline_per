# -*- coding: utf-8 -*-
"""Assemble R4 using the verified embedded CPython3.8 and pinned wheelhouse."""
import argparse
import copy
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from tools.build_product_v2_portable import assemble,copy_file,copy_tree,write_json,seal,EMBED
from tools.product_price_automl_r4 import DEFAULT


def build(stage,examples):
    embed=EMBED
    if ROOT/"deliverables" not in stage.resolve().parents: raise ValueError("交付目录须位于workspace/deliverables下。")
    assemble(stage,embed)
    for name in ("product_price_automl_r4.py","product_r4_input.py","price_attribute_input.py","product_export_saved.py","product_r4_fixture.py","upgrade_product_r41.py","product_r42_search.py","product_r42_profiles.py","upgrade_product_r42.py","product_r43_search.py","product_r43_profiles.py","product_r43_diagnostics.py","upgrade_product_r43.py"):
        copy_file(ROOT/"tools"/name,stage/"tools"/name)
    for name in ('product_price_optuna_v2.py','product_optuna_v2_input.py','upgrade_product_optuna_v2.py'):
        copy_file(ROOT/'tools'/name,stage/'tools'/name)
    copy_tree(ROOT/'services/price_service/optuna_v2',stage/'services/price_service/optuna_v2')
    copy_file(ROOT/'docs/OPTUNA_V2_PIPELINE.md',stage/'docs/OPTUNA_V2_PIPELINE.md')
    from tools.product_price_optuna_v2 import load_config as optuna_config
    write_json(stage/'config/price_optuna_v2.json',optuna_config())
    copy_file(ROOT/"services/price_service/r4_estimators.py",stage/"services/price_service/r4_estimators.py")
    copy_file(ROOT/"services/price_service/r43_support.py",stage/"services/price_service/r43_support.py")
    for name in ("product_price_r4_test.py","product_price_r41_test.py","product_saved_start_test.py"):
        if (ROOT/"tests"/name).is_file(): copy_file(ROOT/"tests"/name,stage/"tests"/name)
    sample=examples/"virtual_10x50_r4.xlsx"
    copy_file(sample,stage/"input/products.xlsx")
    copy_file(sample,stage/"templates/10条50属性_价格评分示例.xlsx")
    copy_file(examples/"virtual_10x40_r4.xlsx",stage/"templates/10条40属性_价格评分示例.xlsx")
    for relative in ("templates/10条50属性_示例及专家配置.xlsx","config/price_automl_v2.json"):
        old=stage/relative
        if old.is_file(): old.unlink()
    config=copy.deepcopy(DEFAULT)
    write_json(stage/"config/price_automl_r4.json",config)
    settings={"product_code":"PORTABLE_PRODUCT","product_name":"成品价格预测与效能推荐R4.1","price_engine":"r4","startup_policy":"saved_only",
              "missing_tokens":[],"demo_input_sha256":hashlib.sha256(sample.read_bytes()).hexdigest(),"ports":{"price":18701,"effectiveness":18702,"recommendation":18791}}
    write_json(stage/"config/portable.json",settings)
    def batch(script,command=""):
        lines=["@echo off","setlocal","chcp 65001 >nul",r'cd /d "%~dp0"',
               '"%~dp0runtime\\python38\\python.exe" -X utf8 "%~dp0tools\\'+script+'" '+command+' %*',
               "if errorlevel 1 (","  if not defined PRODUCT_NO_PAUSE pause","  exit /b 1",
               ")","endlocal",""]
        return "\r\n".join(lines).encode("ascii")
    for name,command in (("START.bat","start"),("TRAIN.bat","build"),("RETRAIN.bat","build"),("REBUILD.bat","build"),("CHECK_INPUT.bat","inspect")):
        (stage/name).write_bytes(batch("product_portable.py",command))
    (stage/"EXPORT.bat").write_bytes(batch("product_export_saved.py"))
    (stage/"APPLY_R4_1_FIX.bat").write_bytes(batch("upgrade_product_r41.py"))
    for name,profile in (('APPLY_OPTUNA_V2.bat','standard'),('APPLY_OPTUNA_V2_QUICK.bat','quick'),('APPLY_OPTUNA_V2_DEEP.bat','deep')):
        (stage/name).write_bytes(batch('upgrade_product_optuna_v2.py','--profile '+profile))
    for name in ("PRICE_ATTRIBUTE_EXPERT_R4_PLAN.md","PRICE_ATTRIBUTE_EXPERT_R4_RESEARCH.md","PRICE_R4_IMPLEMENTED.md","PRICE_R41_AUDIT_FIX.md"):
        if (ROOT/"docs"/name).is_file(): copy_file(ROOT/"docs"/name,stage/"docs"/name)
    copy_file(ROOT/"docs/PRICE_R4_IMPLEMENTED.md",stage/"使用说明.md")
    copy_file(ROOT/"docs/PRICE_R41_AUDIT_FIX.md",stage/"本次算法修复说明.md")
    return stage


def main():
    p=argparse.ArgumentParser(); p.add_argument("--stage",required=True); p.add_argument("--examples",required=True); p.add_argument("--zip")
    args=p.parse_args(); stage=Path(args.stage).resolve()
    if args.zip: print(json.dumps(seal(stage,Path(args.zip).resolve()),ensure_ascii=False,indent=2))
    else: print("ASSEMBLED: "+str(build(stage,Path(args.examples))))
if __name__=="__main__": main()
