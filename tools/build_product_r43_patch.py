# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_r43 import PATCH_FILES
from tools.product_r43_profiles import profile
from tools.product_patch_dependencies import validate_local_imports


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();target=Path(a.output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP名称。')
    entries={rel:(ROOT/rel).read_bytes() for rel in PATCH_FILES if rel.endswith(('.py','.js'))}
    for filename,name in [('APPLY_R4_3_PATCH.bat','data_only'),('APPLY_R4_3_QUICK.bat','quick'),('APPLY_R4_3_EXPERT_COMPARE.bat','expert_compare'),('APPLY_R4_3_LOO_AUDIT.bat','loo_audit')]:
        lines=['@echo off','setlocal','chcp 65001 >nul',r'cd /d "%~dp0"',
               r'"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\upgrade_product_r43.py" --profile '+name+' %*',
               'if errorlevel 1 (','  if not defined PRODUCT_NO_PAUSE pause','  exit /b 1',')','endlocal','']
        entries[filename]='\r\n'.join(lines).encode('ascii')
        entries['config/r43_presets/'+name+'.json']=(json.dumps(profile(name),ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    repair=['@echo off','setlocal','chcp 65001 >nul',r'cd /d "%~dp0"',
            r'"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\upgrade_product_r43.py" --repair-only %*',
            'if errorlevel 1 (','  if not defined PRODUCT_NO_PAUSE pause','  exit /b 1',')','endlocal','']
    entries['APPLY_R4_3_INPUT_FIX.bat']='\r\n'.join(repair).encode('ascii')
    entries['R4_3_PATCH使用说明.md']=(ROOT/'docs/PRICE_R43_PATCH.md').read_bytes()
    entries['R4_3_1缺失模块修复说明.md']=(ROOT/'docs/PRICE_R431_INPUT_FIX.md').read_bytes()
    dependency_report=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013')
    entries['R431_DEPENDENCY_CHECK.json']=(json.dumps(dependency_report,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    entries['R43_PATCH_FILES.json']=(json.dumps({'files':[{'path':rel,'sha256':hashlib.sha256(data).hexdigest(),'bytes':len(data)} for rel,data in entries.items()]},ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    with zipfile.ZipFile(str(target),'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for rel,data in entries.items():z.writestr(rel,data)
    with zipfile.ZipFile(str(target)) as z:assert z.testzip() is None
    result={'status':'PACKAGED','revision':'R4.3.1','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'uses_existing_python38_environment':True,'dependency_check':dependency_report}
    Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8')
    Path(str(target)+'.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
