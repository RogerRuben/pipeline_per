# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_r42 import PATCH_FILES
from tools.product_r42_profiles import profile


def main():
    p=argparse.ArgumentParser();p.add_argument("--output",required=True);a=p.parse_args();target=Path(a.output).resolve()
    if ROOT/"deliverables" not in target.parents or target.exists():raise ValueError("请选择新的项目deliverables ZIP名称。")
    entries={rel:(ROOT/rel).read_bytes() for rel in PATCH_FILES if rel.endswith(('.py','.js'))}
    for filename,name in (("APPLY_R4_2_DATA_ONLY.bat","data_only"),("APPLY_R4_2_QUICK.bat","quick"),("APPLY_R4_2_EXPERT_COMPARE.bat","expert_compare"),("APPLY_R4_2_LOO_AUDIT.bat","loo_audit")):
        lines=['@echo off','setlocal','chcp 65001 >nul',r'cd /d "%~dp0"',
               r'"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\upgrade_product_r42.py" --profile '+name+' %*',
               'if errorlevel 1 (','  if not defined PRODUCT_NO_PAUSE pause','  exit /b 1',')','endlocal','']
        entries[filename]='\r\n'.join(lines).encode('ascii')
        entries['config/r42_presets/'+name+'.json']=(json.dumps(profile(name),ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    entries['R4_2技术方案与使用说明.md']=(ROOT/'docs/PRICE_R42_TECHNICAL_PLAN.md').read_bytes()
    entries['R42_PATCH_FILES.json']=(json.dumps({'files':[{'path':rel,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)} for rel,data in entries.items()]},ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    target.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(str(target),'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for relative,data in entries.items():z.writestr(relative,data)
    with zipfile.ZipFile(str(target)) as z:assert z.testzip() is None
    result={'status':'PACKAGED','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'uses_existing_r41_environment':True}
    Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8')
    Path(str(target)+'.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
