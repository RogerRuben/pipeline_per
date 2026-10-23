# -*- coding: utf-8 -*-
"""One pinned runtime, V2/V3 telemetry, old V4 modes and saved V5 demonstrations."""
import argparse,hashlib,json,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.upgrade_product_v5 import PATCH_FILES
from tools.build_product_v4 import patch_entries as v4_entries
from tools.build_product_optuna_v2_full import batch
from tools.product_patch_dependencies import validate_local_imports
from tools.product_price_optuna_v5 import load_config
from tools.product_modes import descriptor,mode_key


def encoded(value):return (json.dumps(value,ensure_ascii=False,indent=2)+'\n').encode('utf-8')


def base_archive():
    for name in ('ProductAutoML_V4_Offline_20261016.zip','ProductAutoML_V4_Offline.zip'):
        path=ROOT/'deliverables'/name
        if path.is_file():
            metadata=ROOT/'deliverables/ProductAutoML_V4_Offline_20261016.zip.json'
            if metadata.is_file() and hashlib.sha256(path.read_bytes()).hexdigest()!=json.loads(metadata.read_text(encoding='utf-8'))['sha256']:raise ValueError('V4基础离线包摘要与已验收记录不一致。')
            return path
    raise ValueError('缺少已验收V4基础离线包。可使用原日期文件名或ProductAutoML_V4_Offline.zip。')


def patch_entries():
    entries,_=v4_entries()
    for name in PATCH_FILES:
        if not name.endswith('.bat'):entries[name]=(ROOT/name).read_bytes()
    for name,script,args in [('APPLY_V5.bat','upgrade_product_v5.py',''),('APPLY_V5_QUICK.bat','upgrade_product_v5.py','--profile quick'),
        ('APPLY_V5_DEEP.bat','upgrade_product_v5.py','--profile deep'),('SELECT_V5.bat','product_modes.py','select --engine optuna_v5'),
        ('RESUME.bat','product_portable.py','build')]+[('V5_'+m.upper()+'.bat','product_modes.py','select --engine optuna_v5 --strategy '+m) for m in ('goal_first','balanced','robust')]:entries[name]=batch(script,args)
    entries['V5使用说明.md']=entries['docs/OPTUNA_V5_PIPELINE.md']
    dependency=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013',optional_modules={'tools.product_price_tabpfn'})
    entries['OPTUNA_V5_PATCH_FILES.json']=encoded({'format':'optuna-v5-patch-1.0','dependency_check':dependency,
        'files':[{'path':name,'bytes':len(value),'sha256':hashlib.sha256(value).hexdigest()} for name,value in sorted(entries.items())]})
    return entries,dependency


def build(output,full=False,evidence=None):
    target=Path(output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP。')
    entries,dependency=patch_entries();prefix='ProductAutoML_V5'
    if full:
        evidence=Path(evidence).resolve()
        with zipfile.ZipFile(base_archive()) as base:
            old_prefix=base.namelist()[0].split('/')[0]+'/';settings=json.loads(base.read(old_prefix+'config/portable.json').decode('utf-8'))
            registry=json.loads(base.read(old_prefix+'state/modes.json').decode('utf-8'))
        settings.update(price_engine='optuna_v5',product_name='成品价格效能与推荐（V5虚拟演示）',startup_policy='saved_only',mode_registry_enabled=True,engine_selection_required=True)
        entries['config/portable.json']=encoded(settings);entries['config/price_optuna_v5.json']=encoded(load_config())
        for mode in ('balanced','robust','goal_first'):
            directory=evidence/('demo_v5_'+mode);manifest=json.loads((directory/'prototype_manifest.json').read_text(encoding='utf-8'))
            if manifest.get('status')!='ready' or not manifest.get('virtual_data') or manifest['price_engine']!='optuna_v5':raise ValueError('V5演示未完成：'+mode)
            relative='outputs/demo_v5_'+mode
            for path in directory.rglob('*'):
                rel=path.relative_to(directory)
                if not path.is_file() or set(rel.parts)&{'__pycache__','input','backups','checkpoints'} or path.suffix in ('.log','.pyc','.pyo') or path.name=='running.json':continue
                entries[relative+'/'+rel.as_posix()]=path.read_bytes()
            cfg=json.loads((directory/'price/effective_config.json').read_text(encoding='utf-8'));desc=descriptor('optuna_v5',cfg);key=mode_key(desc)
            pointer={'format':'product-portable-latest-1.0','status':'ready','output':relative,'price_engine':'optuna_v5','mode':desc,'mode_key':key,'input_sha256':settings['demo_input_sha256']}
            registry['models'][key]=pointer
            if mode=='goal_first':entries['state/latest.json']=encoded(pointer);entries['state/latest_optuna_v5.json']=encoded(pointer)
        entries['state/modes.json']=encoded(registry)
        entries['使用说明.txt']=('完整离线包：V2/V3增加训练指标日志，V4原模式保留，V5默认保留V3基线、使用预测库融合。\n'
            'START直接运行保存的虚拟演示，不训练；MODE_MENU选择；真实数据替换input/products.xlsx，CHECK_INPUT后RETRAIN。\n'
            'RESUME可恢复同数据检查点；EXPORT导出当前获分模型及环境，不需要原训练表。\n'
            'V5默认300轮完整V3基线；DEEP使用成员fit/时间预算，达标后默认停止新增fit。\n'
            '表格第一行中文、第二行英文唯一代号；专家价格重要性1—5分，属性为数值或配置类型。\n'
            '未达标明确标记试用草稿。虚拟数据成绩不能代表真实业务精度，详见V5使用说明.md。\n').encode('utf-8-sig')
    immutable=[]
    def write(z,name,data):
        z.writestr((prefix+'/' if full else '')+name,data)
        if full and (name.split('/')[0] in ('runtime','app','automl_pipeline','services','tools','docs') or name.endswith('.bat')):immutable.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as archive:
        if full:
            with zipfile.ZipFile(base_archive()) as base:
                for name in base.namelist():
                    relative='/'.join(name.split('/')[1:])
                    if not relative or relative in entries or relative=='PACKAGE_MANIFEST.json':continue
                    write(archive,relative,base.read(name))
        for name,data in entries.items():write(archive,name,data)
        if full:archive.writestr(prefix+'/PACKAGE_MANIFEST.json',encoded({'format':'product-offline-manifest-1.0','training_revision':'Optuna_V5_v3_preserving_1.0','immutable_files':sorted(immutable,key=lambda r:r['path'])}))
    with zipfile.ZipFile(target) as z:assert z.testzip() is None
    value={'status':'BUILT_PENDING_VERIFICATION','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
           'embedded_environment':full,'engines':['optuna_v2','optuna_v3','optuna_v4','optuna_v5'],'dependency_check':dependency}
    Path(str(target)+'.json').write_bytes(encoded(value));Path(str(target)+'.sha256').write_text(value['sha256']+'  '+target.name+'\n',encoding='utf-8');print(json.dumps(value,ensure_ascii=False));return value


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--full',action='store_true');p.add_argument('--evidence');a=p.parse_args();build(a.output,a.full,a.evidence)
if __name__=='__main__':main()
