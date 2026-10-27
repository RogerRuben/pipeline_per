# -*- coding: utf-8 -*-
"""Cumulative V6 + real V21.6, one fixed runtime and fifteen saved modes."""
import argparse,hashlib,json,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.build_product_v51 import patch_entries as previous_entries
from tools.build_product_v5 import encoded
from tools.build_product_optuna_v2_full import batch
from tools.upgrade_product_v6 import PATCH_FILES
from tools.product_patch_dependencies import validate_local_imports
from tools.product_price_optuna_v6 import load_config
from tools.product_modes import descriptor,mode_key

def base_archive():
    meta=json.loads((ROOT/'deliverables/ProductAutoML_V5_1_Offline_20261027.zip.json').read_text(encoding='utf-8'))
    for path in sorted((ROOT/'deliverables').glob('ProductAutoML_V5_1_Offline*.zip')):
        if path.stat().st_size==meta['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==meta['sha256']:return path
    raise ValueError('缺少已验收SHA256一致的V5.1基础离线包。')

def patch_entries():
    entries,_=previous_entries()
    for name in PATCH_FILES:
        if not name.endswith('.bat'):entries[name]=(ROOT/name).read_bytes()
    launchers=[('APPLY_V6.bat','upgrade_product_v6.py',''),('APPLY_V6_QUICK.bat','upgrade_product_v6.py','--profile quick'),
        ('APPLY_V6_DEEP.bat','upgrade_product_v6.py','--profile deep'),('SELECT_V6.bat','product_modes.py','select --engine optuna_v6'),
        ('IMPORT_EFFECTIVENESS.bat','product_effectiveness_import.py','install')]
    launchers += [('V6_'+m.upper()+'.bat','product_modes.py','select --engine optuna_v6 --strategy '+m) for m in ('goal_first','balanced','robust')]
    for name,script,args in launchers:
        entries[name]=batch(script,args)
        (ROOT/name).write_bytes(entries[name])
    entries['V6使用说明.md']=entries['docs/OPTUNA_V6_PIPELINE.md']
    for key in list(entries):
        if key.startswith('OPTUNA_') and key.endswith('_PATCH_FILES.json'):entries.pop(key)
    dependency=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013',optional_modules={'tools.product_price_tabpfn'})
    entries['OPTUNA_V6_PATCH_FILES.json']=encoded({'format':'optuna-v6-patch-1.0','dependency_check':dependency,
        'files':[{'path':n,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in sorted(entries.items())]})
    return entries,dependency

def build(output,full=False,evidence=None):
    target=Path(output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP。')
    entries,dependency=patch_entries();prefix='ProductAutoML_V6';immutable=[]
    if full:
        with zipfile.ZipFile(base_archive()) as z:
            p=z.namelist()[0].split('/')[0]+'/'
            settings=json.loads(z.read(p+'config/portable.json').decode('utf-8'));registry=json.loads(z.read(p+'state/modes.json').decode('utf-8'))
        sample=(ROOT/'automl_pipeline/examples/virtual_v6_16x36.xlsx').read_bytes()
        settings.update(price_engine='optuna_v6',product_name='成品价格效能与推荐（V6虚拟演示）',demo_input_sha256=hashlib.sha256(sample).hexdigest())
        settings['ports']['cost_effectiveness']=18793
        entries['config/portable.json']=encoded(settings);entries['input/products.xlsx']=sample
        entries['config/price_optuna_v6.json']=encoded(load_config())
        for mode in ('goal_first','balanced','robust'):
            demo=Path(evidence)/('demo_v6_'+mode)
            manifest=json.loads((demo/'prototype_manifest.json').read_text(encoding='utf-8'))
            if manifest.get('status')!='ready' or manifest.get('price_engine')!='optuna_v6' or not manifest.get('virtual_data'):raise ValueError('未完成V6演示：'+mode)
            cfg=json.loads((demo/'price/effective_config.json').read_text(encoding='utf-8'));relative='outputs/demo_v6_'+mode
            for path in demo.rglob('*'):
                rel=path.relative_to(demo)
                if not path.is_file() or set(rel.parts)&{'__pycache__','input','backups','checkpoints','transient_search'} or path.suffix in ('.pyc','.pyo','.log') or path.name=='running.json':continue
                entries[relative+'/'+rel.as_posix()]=path.read_bytes()
            desc=descriptor('optuna_v6',cfg);key=mode_key(desc)
            pointer={'format':'product-portable-latest-1.0','status':'ready','output':relative,'price_engine':'optuna_v6','mode':desc,'mode_key':key,'input_sha256':settings['demo_input_sha256']}
            registry['models'][key]=pointer
            if mode=='goal_first':entries['state/latest.json']=encoded(pointer);entries['state/latest_optuna_v6.json']=encoded(pointer)
        entries['state/modes.json']=encoded(registry)
        entries['使用说明.txt']=('V6独立训练/封存验收 + 推荐系统V21.6 + 效费分析台。\nSTART运行保存的虚拟演示，不训练。\n'
            '真实使用：替换input/products.xlsx，CHECK_INPUT后RETRAIN。MODE_MENU支持旧版本与V6三模式。\n'
            'EXPORT导出当前模型、数据和固定环境；不需要原训练表。IMPORT_EFFECTIVENESS导入专家效能组件，不重训价格。\n'
            '窗口关闭一并释放价格、效能、推荐及效费分析服务。详情见V6使用说明.md。\n虚拟精度不代表真实业务精度。\n').encode('utf-8-sig')
    def write(z,name,data):
        z.writestr((prefix+'/' if full else '')+name,data)
        if full and (name.split('/')[0] in ('runtime','app','automl_pipeline','services','tools','docs','cost_effectiveness_analysis') or name.endswith('.bat')):
            immutable.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        if full:
            with zipfile.ZipFile(base_archive()) as old:
                for name in old.namelist():
                    relative='/'.join(name.split('/')[1:])
                    if relative and relative not in entries and relative!='PACKAGE_MANIFEST.json':write(z,relative,old.read(name))
        for name,data in entries.items():write(z,name,data)
        if full:z.writestr(prefix+'/PACKAGE_MANIFEST.json',encoded({'format':'product-offline-manifest-1.0','training_revision':'Optuna_V6_sealed_1.0',
            'recommendation_version':'V21.6','immutable_files':sorted(immutable,key=lambda r:r['path'])}))
    with zipfile.ZipFile(target) as z:assert z.testzip() is None
    result={'status':'BUILT_PENDING_VERIFICATION','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
        'embedded_environment':full,'dependency_check':dependency,'training_revision':'V6-sealed','recommendation_version':'V21.6','business_accuracy_certified':False}
    Path(str(target)+'.json').write_bytes(encoded(result));Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--full',action='store_true');p.add_argument('--evidence');a=p.parse_args();build(a.output,a.full,a.evidence)

if __name__=='__main__':main()
