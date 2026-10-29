# -*- coding: utf-8 -*-
"""Export one fitted version and its pinned environment; never invoke training."""
import argparse
import hashlib
import json
import sys
import zipfile
from datetime import datetime
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from tools import product_portable
from tools.product_saved_start import saved_start_launcher_bytes


def export_saved(target=None,version=None):
    product_portable.verify_runtime()
    source=product_portable.latest_output() if version is None else Path(version).resolve()
    if source is None or not product_portable.inside(source,ROOT/"outputs"): raise ValueError("请选择完整的包内已训练版本。")
    manifest=json.loads((source/"prototype_manifest.json").read_text(encoding="utf-8"))
    if manifest.get("status")!="ready": raise ValueError("该版本尚未完成导出。")
    for key in ("price_model","effectiveness_model","business_package"):
        path=(source/manifest[key]).resolve()
        if not product_portable.inside(path,source) or product_portable.digest(path)!=manifest["artifact_sha256"][key]: raise ValueError("保存模型校验失败："+key)
    timestamp=datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix={'optuna_v2':'ProductOptunaV2_Saved_','optuna_v3':'ProductOptunaV3_Saved_','optuna_v4':'ProductOptunaV4_Saved_','optuna_v5':'ProductOptunaV5_Saved_','optuna_v6':'ProductOptunaV6_Saved_','optuna_v7':'ProductOptunaV7_Saved_'}.get(manifest.get('price_engine'),'ProductR4_Saved_')
    target=Path(target).resolve() if target else ROOT/"exports"/(prefix+timestamp+".zip")
    if target.exists(): raise ValueError("导出文件已存在，请换一个名称。")
    if product_portable.inside(target,source): raise ValueError("导出ZIP不能放进被导出的版本目录。")
    target.parent.mkdir(parents=True,exist_ok=True)
    settings=json.loads((ROOT/"config"/"portable.json").read_text(encoding="utf-8"))
    settings.update(startup_policy="saved_only",engine_selection_required=False,mode_registry_enabled=False,price_engine=manifest.get('price_engine',settings.get('price_engine')))
    relative=source.relative_to(ROOT).as_posix(); entries={}
    skip={"__pycache__",".pytest_cache","oof_cache","node_modules","backups","transient_search","checkpoints"}
    def add(directory,prefix):
        for path in directory.rglob("*"):
            if not path.is_file() or path.is_symlink() or set(path.relative_to(directory).parts)&skip or path.suffix in (".pyc",".pyo",".log"): continue
            if prefix==relative and (path.relative_to(directory).parts[0] in ("input","nested","development") or path.name=="running.json"): continue
            entries[prefix+"/"+path.relative_to(directory).as_posix()]=path
    for folder in ("runtime","app","services","tools","automl_pipeline","cost_effectiveness_analysis"):
        add(ROOT/folder,folder)
    add(source,relative)
    package_name=prefix+timestamp
    with zipfile.ZipFile(str(target),"w",compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
        for name,path in sorted(entries.items()): archive.write(str(path),package_name+"/"+name)
        archive.writestr(package_name+"/START.bat",saved_start_launcher_bytes())
        archive.writestr(package_name+"/RUN_SAVED.bat",saved_start_launcher_bytes())
        importer='\r\n'.join(['@echo off','setlocal','chcp 65001 >nul',r'"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\product_effectiveness_import.py" install %*','if errorlevel 1 (','  if not defined PRODUCT_NO_PAUSE pause','  exit /b 1',')','endlocal',''])
        archive.writestr(package_name+'/IMPORT_EFFECTIVENESS.bat',importer.encode('ascii'))
        archive.writestr(package_name+"/config/portable.json",json.dumps(settings,ensure_ascii=False,indent=2))
        archive.writestr(package_name+"/state/latest.json",json.dumps({"status":"ready","output":relative,"format":"product-portable-latest-1.0"},indent=2))
        note='已训练运行包。双击START.bat载入保存模型。无需原训练表，无训练入口。运行环境、价格/效能模型和推荐数据随包保留。\n'
        if manifest.get('virtual_data'):note+='本包为虚拟数据演示；真实业务模型应使用自己的成品表训练后重新导出。\n'
        archive.writestr(package_name+"/使用说明.txt",note.encode("utf-8-sig"))
    sha=hashlib.sha256(target.read_bytes()).hexdigest()
    Path(str(target)+".sha256").write_text(sha+"  "+target.name+"\n",encoding="utf-8")
    product_portable.write_json(Path(str(target)+".json"),{"status":"EXPORTED","path":str(target),"sha256":sha,"source_version":relative,
                               "training_called":False,"embedded_environment":True,"original_training_table_required":False,
                               "price_engine":manifest.get('price_engine'),"virtual_data":bool(manifest.get('virtual_data'))})
    return target


def main():
    p=argparse.ArgumentParser(); p.add_argument("--output"); p.add_argument("--version"); args=p.parse_args()
    try: print("已导出保存模型与环境：%s"%export_saved(args.output,args.version)); return 0
    except Exception as exc: print("导出失败：%s"%exc,file=sys.stderr); return 1
if __name__=="__main__": sys.exit(main())
