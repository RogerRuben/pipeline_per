# -*- coding: utf-8 -*-
"""Compose one offline environment with V2, V3 and four saved V4 demonstrations."""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_v4 import PATCH_FILES
from tools.build_product_optuna_dual import patch_entries as dual_entries,json_bytes
from tools.build_product_optuna_v2_full import batch
from tools.product_saved_start import saved_start_launcher_bytes
from tools.product_patch_dependencies import validate_local_imports
from tools.product_price_optuna_v2 import load_config as v2_config
from tools.product_price_optuna_v3 import load_config as v3_config
from tools.product_price_optuna_v4 import load_config as v4_config
from tools.product_modes import descriptor,mode_key


def patch_entries():
    entries,_=dual_entries(validate=False)
    for name in PATCH_FILES:
        if not name.endswith('.bat'):entries[name]=(ROOT/name).read_bytes()
    for name,script,args in [('APPLY_V4.bat','upgrade_product_v4.py',''),('APPLY_V4_QUICK.bat','upgrade_product_v4.py','--profile quick'),
       ('APPLY_V4_DEEP.bat','upgrade_product_v4.py','--profile deep'),('SELECT_V4.bat','product_modes.py','select --engine optuna_v4'),
       ('MODE_MENU.bat','product_modes.py','menu')]+[('V4_'+mode.upper()+'.bat','product_modes.py','select --engine optuna_v4 --strategy '+mode) for mode in ('random','global','per_split','hybrid')]:entries[name]=batch(script,args)
    entries['V4使用说明.md']=entries['docs/OPTUNA_V4_PIPELINE.md']
    dependencies=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013',optional_modules={'tools.product_price_tabpfn'})
    entries['OPTUNA_V4_PATCH_FILES.json']=json_bytes({'format':'optuna-v4-patch-1.0','dependency_check':dependencies,'files':[{'path':n,'bytes':len(d),'sha256':hashlib.sha256(d).hexdigest()} for n,d in sorted(entries.items())]})
    return entries,dependencies


def build(output,full=False,evidence=None):
    target=Path(output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP。')
    entries,dependency=patch_entries();prefix='ProductAutoML_V4'
    if full:
        evidence=Path(evidence).resolve();sample=ROOT/'outputs/optuna_dual_20261016/products.xlsx'
        entries['input/products.xlsx']=sample.read_bytes();entries['templates/16条36属性_两种类型及价格评分.xlsx']=sample.read_bytes()
        for engine,load in (('optuna_v2',v2_config),('optuna_v3',v3_config),('optuna_v4',v4_config)):entries['config/price_'+engine+'.json']=json_bytes(load())
        settings={'product_code':'PORTABLE_PRODUCT','product_name':'成品价格效能与推荐（V4虚拟演示）','price_engine':'optuna_v4','startup_policy':'saved_only','engine_selection_required':True,'mode_registry_enabled':True,
                  'missing_tokens':[],'demo_input_sha256':hashlib.sha256(sample.read_bytes()).hexdigest(),'ports':{'price':18701,'effectiveness':18702,'recommendation':18791}}
        entries['config/portable.json']=json_bytes(settings);registry={'format':'price-saved-modes-1.0','models':{}}
        for short in ('v2','v3','v4_random','v4_global','v4_per_split','v4_hybrid'):
            directory=(ROOT/'outputs/optuna_dual_20261016' if short in ('v2','v3') else evidence)/('demo_'+short)
            manifest=json.loads((directory/'prototype_manifest.json').read_text(encoding='utf-8'));engine=manifest['price_engine']
            if manifest['status']!='ready' or not manifest['virtual_data']:raise ValueError('演示未完成：'+short)
            relative='outputs/demo_'+short
            for path in directory.rglob('*'):
                rel=path.relative_to(directory)
                if not path.is_file() or set(rel.parts)&{'__pycache__','backups','input'} or path.suffix in ('.pyc','.pyo','.log') or path.name=='running.json':continue
                entries[relative+'/'+rel.as_posix()]=path.read_bytes()
            cfg=json.loads((directory/'price/effective_config.json').read_text(encoding='utf-8'));desc=descriptor(engine,cfg);key=mode_key(desc)
            pointer={'format':'product-portable-latest-1.0','status':'ready','output':relative,'price_engine':engine,'input_sha256':settings['demo_input_sha256'],'mode':desc,'mode_key':key}
            registry['models'][key]=pointer;entries['state/latest_'+engine+'.json']=json_bytes(pointer)
            if short=='v4_hybrid':entries['state/latest.json']=json_bytes(pointer)
        entries['state/modes.json']=json_bytes(registry)
        entries['使用说明.txt']=('完整离线包：V2、V3和V4四种搜索模式，共用一个环境，六套已保存虚拟演示模型。\n'
            'START.bat直接运行保存模型；MODE_MENU.bat切换所有模式；不自动重训。\n'
            '真实使用：替换input/products.xlsx，CHECK_INPUT.bat，再RETRAIN.bat。EXPORT.bat导出当前成品与环境。\n'
            '双行表头：中文名、唯一英文代号。专家价格重要性1—5分；数值或配置类型。可选效能、耦合表不必填写。\n'
            'V4默认混合复核；训练及开发MAPE≤10%、R²≥0.9；未达标明确标记试用草稿。\n'
            '详见V4使用说明.md；虚拟数据成绩不代表真实业务性能。\n').encode('utf-8-sig')
    immutable=[]
    def write(archive,name,data):
        archive.writestr((prefix+'/' if full else '')+name,data)
        if full and (name.split('/')[0] in ('runtime','app','services','tools','docs','automl_pipeline') or name.endswith('.bat')):immutable.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    with zipfile.ZipFile(str(target),'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
        if full:
            with zipfile.ZipFile(str(ROOT/'deliverables/ProductOptunaV2_Full_Offline_20261015.zip')) as old:
                for name in old.namelist():
                    relative='/'.join(name.split('/')[1:])
                    if not relative or relative in entries or relative=='PACKAGE_MANIFEST.json' or relative.split('/')[0] in ('outputs','state','input','templates','config') or relative.endswith('.json.sha256'):continue
                    write(archive,relative,old.read(name))
        for name,data in entries.items():write(archive,name,data)
        if full:archive.writestr(prefix+'/PACKAGE_MANIFEST.json',json_bytes({'format':'product-offline-manifest-1.0','training_revision':'Optuna_V4_switchable_1.0','immutable_files':sorted(immutable,key=lambda r:r['path'])}))
    with zipfile.ZipFile(str(target)) as archive:assert archive.testzip() is None
    result={'status':'BUILT_PENDING_VERIFICATION','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),'embedded_environment':full,'engines':['optuna_v2','optuna_v3','optuna_v4'],'dependency_check':dependency}
    Path(str(target)+'.json').write_bytes(json_bytes(result));Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8');print(json.dumps(result,ensure_ascii=False));return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--full',action='store_true');p.add_argument('--evidence');a=p.parse_args();build(a.output,a.full,a.evidence)
if __name__=='__main__':main()
