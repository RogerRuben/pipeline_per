# -*- coding: utf-8 -*-
"""Cumulative V5.1 PATCH and one-runtime offline package, preserving old modes."""
import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.build_product_v5 import patch_entries as v5_entries,encoded
from tools.build_product_optuna_v2_full import batch
from tools.upgrade_product_v51 import PATCH_FILES
from tools.product_patch_dependencies import validate_local_imports
from tools.product_price_optuna_v5 import load_config
from tools.product_modes import descriptor,mode_key


def base_archive():
    meta_path=ROOT/'deliverables/ProductAutoML_V5_Offline_20261023.zip.json'
    meta=json.loads(meta_path.read_text(encoding='utf-8'))
    for path in sorted((ROOT/'deliverables').glob('ProductAutoML_V5_Offline*.zip')):
        if path.stat().st_size==meta['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==meta['sha256']:
            return path
    raise ValueError('缺少与已验收SHA256一致的V5基础包（允许更改日期文件名）。')


def patch_entries():
    entries,_=v5_entries()
    for name in PATCH_FILES:
        if not name.endswith('.bat'):entries[name]=(ROOT/name).read_bytes()
    for name,arguments in [('APPLY_V5_1.bat',''),('APPLY_V5_1_QUICK.bat','--profile quick'),('APPLY_V5_1_DEEP.bat','--profile deep'),
                           ('APPLY_V5.bat',''),('APPLY_V5_QUICK.bat','--profile quick'),('APPLY_V5_DEEP.bat','--profile deep')]:
        entries[name]=batch('upgrade_product_v51.py',arguments)
    entries['V5_FIXED_CV.bat']=batch('product_modes.py','select --engine optuna_v5 --protocol fixed_cv')
    entries['V5_LEGACY.bat']=batch('product_modes.py','select --engine optuna_v5 --protocol legacy_split --scope selected_split')
    entries['V5_1使用说明.md']=entries['docs/OPTUNA_V5_1_PIPELINE.md']
    dependency=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013',optional_modules={'tools.product_price_tabpfn'})
    entries.pop('OPTUNA_V5_PATCH_FILES.json',None)
    entries['OPTUNA_V5_1_PATCH_FILES.json']=encoded({'format':'optuna-v5.1-patch-1.0','dependency_check':dependency,
        'files':[{'path':name,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()} for name,data in sorted(entries.items())]})
    return entries,dependency


def build(output,full=False,evidence=None):
    target=Path(output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP。')
    entries,dependency=patch_entries();prefix='ProductAutoML_V5_1';immutable=[]
    base_path=base_archive() if full else None
    if full:
        with zipfile.ZipFile(base_path) as z:
            old_prefix=z.namelist()[0].split('/')[0]+'/'
            settings=json.loads(z.read(old_prefix+'config/portable.json').decode('utf-8'))
            registry=json.loads(z.read(old_prefix+'state/modes.json').decode('utf-8'))
        settings.update(price_engine='optuna_v5',product_name='成品价格效能与推荐（V5.1固定CV虚拟演示）')
        entries['config/portable.json']=encoded(settings);entries['config/price_optuna_v5.json']=encoded(load_config())
        for mode in ('balanced','robust','goal_first'):
            demo=Path(evidence)/('demo_v51_'+mode)
            manifest=json.loads((demo/'prototype_manifest.json').read_text(encoding='utf-8'))
            if manifest.get('status')!='ready' or not manifest.get('virtual_data'):raise ValueError('演示未完成：'+mode)
            cfg=json.loads((demo/'price/effective_config.json').read_text(encoding='utf-8'))
            if cfg['v5']['protocol']!='fixed_cv':raise ValueError('必须使用新固定CV演示。')
            relative='outputs/demo_v51_'+mode
            for path in demo.rglob('*'):
                rel=path.relative_to(demo)
                if not path.is_file() or set(rel.parts)&{'__pycache__','input','backups','checkpoints'} or path.suffix in ('.log','.pyc','.pyo') or path.name in ('running.json','training_events.jsonl','enhancement_trials.jsonl'):continue
                entries[relative+'/'+rel.as_posix()]=path.read_bytes()
            desc=descriptor('optuna_v5',cfg);key=mode_key(desc)
            pointer={'format':'product-portable-latest-1.0','status':'ready','output':relative,'price_engine':'optuna_v5','mode':desc,'mode_key':key,'input_sha256':settings['demo_input_sha256']}
            registry['models'][key]=pointer
            if mode=='goal_first':entries['state/latest.json']=encoded(pointer);entries['state/latest_optuna_v5.json']=encoded(pointer)
        entries['state/modes.json']=encoded(registry)
        entries['使用说明.txt']=('V5.1固定CV：新增阶段冻结融合权重，每轮替换一个模型族，固定覆盖全部样本的折。\n'
            'START直接运行保存的虚拟演示；换input/products.xlsx后CHECK_INPUT、RETRAIN。RESUME可续训；EXPORT含模型及环境。\n'
            '所有价格阶段有完整日志和心跳。V2/V3/V4/旧V5及新V5.1可切换；详见V5_1使用说明.md。\n'
            '开发成绩参与选择，不是独立测试精度。未达标明确为试用草稿。\n').encode('utf-8-sig')
    def write(z,name,data):
        z.writestr((prefix+'/' if full else '')+name,data)
        if full and (name.split('/')[0] in ('runtime','app','automl_pipeline','services','tools','docs') or name.endswith('.bat')):
            immutable.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        if full:
            with zipfile.ZipFile(base_path) as old:
                for name in old.namelist():
                    relative='/'.join(name.split('/')[1:])
                    if relative and relative not in entries and relative!='PACKAGE_MANIFEST.json':write(z,relative,old.read(name))
        for name,data in entries.items():write(z,name,data)
        if full:z.writestr(prefix+'/PACKAGE_MANIFEST.json',encoded({'format':'product-offline-manifest-1.0','training_revision':'Optuna_V5_1_fixed_cv_1.0','immutable_files':sorted(immutable,key=lambda r:r['path'])}))
    with zipfile.ZipFile(target) as z:assert z.testzip() is None
    result={'status':'BUILT_PENDING_VERIFICATION','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'embedded_environment':full,'dependency_check':dependency,'training_revision':'V5.1-fixed-cv','business_accuracy_certified':False}
    Path(str(target)+'.json').write_bytes(encoded(result));Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));return result


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--full',action='store_true');p.add_argument('--evidence');a=p.parse_args()
    build(a.output,a.full,a.evidence)


if __name__=='__main__':main()
