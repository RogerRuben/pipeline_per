# -*- coding: utf-8 -*-
"""Compose a trainable offline package from the verified exported runtime + patch."""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.product_price_optuna_v2 import load_config


def batch(script,args=''):
    return ('\r\n'.join(['@echo off','setlocal','chcp 65001 >nul',r'cd /d "%~dp0"',
       '"%~dp0runtime\\python38\\python.exe" -X utf8 "%~dp0tools\\'+script+'" '+args+' %*',
       'if errorlevel 1 (','  if not defined PRODUCT_NO_PAUSE pause','  exit /b 1',')','endlocal',''])).encode('ascii')


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();target=Path(a.output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('Choose a new deliverables ZIP')
    saved=ROOT/'deliverables/ProductOptunaV2_Saved_Demo_20261015.zip';patch=ROOT/'deliverables/ProductOptunaV2_Pipeline_PATCH_20261015.zip'
    with zipfile.ZipFile(str(patch)) as z:overlay={name:z.read(name) for name in z.namelist()}
    cfg=load_config();overlay['config/price_optuna_v2.json']=(json.dumps(cfg,ensure_ascii=False,indent=2)+'\n').encode('utf-8')
    overlay['input/products.xlsx']=(ROOT/'outputs/optuna_v2_pipeline_20261015/training_evidence/verified_virtual_16x36.xlsx').read_bytes()
    overlay['templates/16条36属性_双行表头及价格评分.xlsx']=overlay['input/products.xlsx']
    for name,script,args in [('RETRAIN.bat','product_portable.py','build'),('TRAIN.bat','product_portable.py','build'),('CHECK_INPUT.bat','product_portable.py','inspect'),('EXPORT.bat','product_export_saved.py','')]:overlay[name]=batch(script,args)
    overlay['使用说明.txt']=('完整离线训练与运行包，已保存虚拟16条×36属性演示模型。\n'
        '解压到较短目录后，START.bat可直接演示。处理真实数据时，只替换input/products.xlsx，再运行CHECK_INPUT.bat和RETRAIN.bat。\n'
        '表格第一行中文名，第二行英文唯一代号；价格专家评分为1—5分。效能和耦合表可不填。\n'
        '默认150轮联合搜索，参数在config/price_optuna_v2.json；APPLY_OPTUNA_V2_DEEP.bat设5000轮。\n'
        '后续START只加载保存模型；EXPORT.bat导出带环境的已训练纯运行包，无需训练表。\n'
        '当前演示只验证软件功能，不代表真实业务精度。详见docs/OPTUNA_V2_PIPELINE.md。\n').encode('utf-8-sig')
    prefix='ProductOptunaV2';immutable=[]
    def write(z,name,data):
        z.writestr(prefix+'/'+name,data)
        if name.split('/')[0] in ('runtime','app','services','tools','docs') or name.endswith('.bat'):
            immutable.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    with zipfile.ZipFile(str(saved)) as old,zipfile.ZipFile(str(target),'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as out:
        for name in old.namelist():
            relative='/'.join(name.split('/')[1:])
            if not relative or relative in overlay or relative=='PACKAGE_MANIFEST.json':continue
            write(out,relative,old.read(name))
        for name,data in overlay.items():write(out,name,data)
        manifest={'format':'product-offline-manifest-1.0','training_revision':'supplied_Optuna_V2_pipeline_1.0','immutable_files':sorted(immutable,key=lambda r:r['path'])}
        out.writestr(prefix+'/PACKAGE_MANIFEST.json',json.dumps(manifest,ensure_ascii=False,indent=2))
    with zipfile.ZipFile(str(target)) as z:assert z.testzip() is None
    result={'status':'BUILT_PENDING_VERIFICATION','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
       'python_environment_included':True,'trainable':True,'saved_virtual_demo':True,'default_trials':150,'source_saved_zip_sha256':hashlib.sha256(saved.read_bytes()).hexdigest(),'source_patch_sha256':hashlib.sha256(patch.read_bytes()).hexdigest()}
    Path(str(target)+'.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
