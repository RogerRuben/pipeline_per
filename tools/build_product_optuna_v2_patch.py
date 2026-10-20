# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_optuna_v2 import PATCH_FILES
from tools.product_price_optuna_v2 import load_config
from tools.product_patch_dependencies import validate_local_imports


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();target=Path(a.output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP名称。')
    entries={name:(ROOT/name).read_bytes() for name in PATCH_FILES if not name.endswith('.bat')}
    for file,profile,count in [('APPLY_OPTUNA_V2.bat','standard',150),('APPLY_OPTUNA_V2_QUICK.bat','quick',10),('APPLY_OPTUNA_V2_DEEP.bat','deep',5000)]:
        rows=['@echo off','setlocal','chcp 65001 >nul',r'cd /d "%~dp0"',
              r'"%~dp0runtime\python38\python.exe" -X utf8 "%~dp0tools\upgrade_product_optuna_v2.py" --profile '+profile+' %*',
              'if errorlevel 1 (','  if not defined PRODUCT_NO_PAUSE pause','  exit /b 1',')','endlocal','']
        entries[file]='\r\n'.join(rows).encode('ascii')
        cfg=load_config(overrides={'search':{'n_trials':count}})
        entries['config/optuna_v2_presets/'+profile+'.json']=(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    entries['OPTUNA_V2使用说明.md']=entries['docs/OPTUNA_V2_PIPELINE.md']
    # Existing TabPFN is a separate, explicitly selected Python3.11 product.
    # The optuna_v2 branch cannot reach its conditional import.
    dependency=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013',optional_modules={'tools.product_price_tabpfn'})
    entries['OPTUNA_V2_PATCH_FILES.json']=(json.dumps({'engine':'optuna_v2','supplied_archive_sha256':'531ed2fe033b1178910aeda812e35edac8c2f93057af158c82bafd282292947c','dependency_check':dependency,
            'files':[{'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in sorted(entries.items())]},ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    with zipfile.ZipFile(str(target),'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for name,data in entries.items():z.writestr(name,data)
    with zipfile.ZipFile(str(target)) as z:assert z.testzip() is None
    result={'status':'PACKAGED','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'reuses_existing_python38_environment':True,'dependency_check':dependency}
    Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8')
    Path(str(target)+'.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
