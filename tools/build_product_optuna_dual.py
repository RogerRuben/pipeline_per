# -*- coding: utf-8 -*-
"""One offline environment with selectable V2/V3 engines and separate saved demos."""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_optuna_dual import PATCH_FILES
from tools.build_product_optuna_v2_full import batch
from tools.product_price_optuna_v2 import load_config as v2_config
from tools.product_price_optuna_v3 import load_config as v3_config
from tools.product_saved_start import saved_start_launcher_bytes
from tools.product_patch_dependencies import validate_local_imports


def json_bytes(value):return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')


def patch_entries(validate=True):
    entries={name:(ROOT/name).read_bytes() for name in PATCH_FILES if not name.endswith('.bat')}
    for name,script,args in (
        ('APPLY_V2_V3.bat','upgrade_product_optuna_dual.py',''),
        ('APPLY_V2_V3_QUICK.bat','upgrade_product_optuna_dual.py','--profile quick'),
        ('APPLY_V2_V3_DEEP.bat','upgrade_product_optuna_dual.py','--profile deep'),
        ('SELECT_V2.bat','product_optuna_versions.py','select --engine optuna_v2'),
        ('SELECT_V3.bat','product_optuna_versions.py','select --engine optuna_v3'),
        ('COMPARE_V2_V3.bat','product_optuna_versions.py','compare'),
        ('APPLY_OPTUNA_V2.bat','upgrade_product_optuna_v2.py',''),
        ('APPLY_OPTUNA_V2_QUICK.bat','upgrade_product_optuna_v2.py','--profile quick'),
        ('APPLY_OPTUNA_V2_DEEP.bat','upgrade_product_optuna_v2.py','--profile deep')):
        entries[name]=batch(script,args)
    for name,command in (('RETRAIN.bat','build'),('TRAIN.bat','build'),('CHECK_INPUT.bat','inspect')):entries[name]=batch('product_portable.py',command)
    entries['EXPORT.bat']=batch('product_export_saved.py');entries['START.bat']=saved_start_launcher_bytes();entries['RUN_SAVED.bat']=saved_start_launcher_bytes()
    dependency=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013',optional_modules={'tools.product_price_tabpfn'}) if validate else {'status':'deferred_to_composed_payload'}
    entries['V2_V3使用说明.md']=entries['docs/OPTUNA_V2_V3_PIPELINE.md']
    entries['OPTUNA_V2使用说明.md']=entries['docs/OPTUNA_V2_PIPELINE.md']
    entries['OPTUNA_DUAL_PATCH_FILES.json']=json_bytes({'format':'optuna-dual-patch-1.0','dependency_check':dependency,
        'files':[{'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in sorted(entries.items())]})
    return entries,dependency


def build(output,full=False,evidence=None):
    target=Path(output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP。')
    entries,dependency=patch_entries();prefix='ProductAutoML_V2_V3'
    if full:
        evidence=Path(evidence).resolve();sample=evidence/'products.xlsx'
        entries['input/products.xlsx']=sample.read_bytes();entries['templates/16条36属性_两种类型及价格评分.xlsx']=sample.read_bytes()
        entries['config/price_optuna_v2.json']=json_bytes(v2_config());entries['config/price_optuna_v3.json']=json_bytes(v3_config())
        settings={'product_code':'PORTABLE_PRODUCT','product_name':'成品价格效能与推荐（V2/V3虚拟演示）','price_engine':'optuna_v3',
            'startup_policy':'saved_only','engine_selection_required':True,'missing_tokens':[],
            'demo_input_sha256':hashlib.sha256(sample.read_bytes()).hexdigest(),'ports':{'price':18701,'effectiveness':18702,'recommendation':18791}}
        entries['config/portable.json']=json_bytes(settings)
        for short in ('v2','v3'):
            directory=evidence/('demo_'+short);engine='optuna_'+short
            manifest=json.loads((directory/'prototype_manifest.json').read_text(encoding='utf-8'))
            if manifest['status']!='ready' or manifest['price_engine']!=engine or not manifest['virtual_data']:raise ValueError('演示模型未完整验证：'+engine)
            relative='outputs/demo_'+short
            for path in directory.rglob('*'):
                rel=path.relative_to(directory)
                if not path.is_file() or set(rel.parts)&{'__pycache__','backups','input'} or path.suffix in ('.pyc','.pyo','.log') or path.name=='running.json':continue
                entries[relative+'/'+rel.as_posix()]=path.read_bytes()
            pointer={'format':'product-portable-latest-1.0','status':'ready','output':relative,'price_engine':engine,'input_sha256':settings['demo_input_sha256']}
            entries['state/latest_'+engine+'.json']=json_bytes(pointer)
            if short=='v3':entries['state/latest.json']=json_bytes(pointer)
        entries['使用说明.txt']=('本包包含V2和V3、共用离线环境和两套已保存的虚拟演示模型。\n'
            '1. SELECT_V2.bat或SELECT_V3.bat选择版本；START.bat直接运行该版本的保存模型。\n'
            '2. 使用真实数据时只替换input/products.xlsx，CHECK_INPUT检查，再RETRAIN训练。\n'
            '3. COMPARE_V2_V3.bat以同条件各300轮训练比较两版。\n'
            '4. EXPORT.bat导出当前选择的成品和环境。\n'
            '属性只用数值和配置类型。专家评分为价格属性的1—5分。可选效能或耦合表不需要强行填写。\n'
            '后续START不会重新训练；两版模型分别保留。详见V2_V3使用说明.md。\n'
            '演示分数来自虚拟数据，不代表真实业务性能。\n').encode('utf-8-sig')
    immutable=[]
    def write(archive,name,data):
        archive.writestr((prefix+'/' if full else '')+name,data)
        if full and (name.split('/')[0] in ('runtime','app','services','tools','docs','automl_pipeline') or name.endswith('.bat')):
            immutable.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    with zipfile.ZipFile(str(target),'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
        if full:
            base=ROOT/'deliverables/ProductOptunaV2_Full_Offline_20261015.zip'
            with zipfile.ZipFile(str(base)) as old:
                for name in old.namelist():
                    relative='/'.join(name.split('/')[1:])
                    if not relative or relative in entries or relative=='PACKAGE_MANIFEST.json' or relative.split('/')[0] in ('outputs','state','input','templates','config') or relative.endswith('.json.sha256'):continue
                    write(archive,relative,old.read(name))
        for name,data in entries.items():write(archive,name,data)
        if full:archive.writestr(prefix+'/PACKAGE_MANIFEST.json',json_bytes({'format':'product-offline-manifest-1.0','training_revision':'Optuna_V2_V3_two_type_1.0','immutable_files':sorted(immutable,key=lambda r:r['path'])}))
    with zipfile.ZipFile(str(target)) as archive:assert archive.testzip() is None
    result={'status':'BUILT_PENDING_VERIFICATION','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
        'full_offline_environment':full,'engines':['optuna_v2','optuna_v3'],'dependency_check':dependency}
    Path(str(target)+'.json').write_bytes(json_bytes(result));Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--full',action='store_true');p.add_argument('--evidence');a=p.parse_args()
    build(a.output,a.full,a.evidence)
if __name__=='__main__':main()
