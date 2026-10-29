# -*- coding: utf-8 -*-
"""One-runtime V7/V3+ offline release and cumulative overlay."""
import argparse,hashlib,json,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.build_product_v6 import patch_entries as previous_entries
from tools.build_product_v5 import encoded
from tools.build_product_optuna_v2_full import batch
from tools.upgrade_product_v7 import PATCH_FILES
from tools.product_patch_dependencies import validate_local_imports
from tools.product_price_optuna_v7 import load_config
from tools.product_modes import descriptor,mode_key

def base_archive():
    meta=json.loads((ROOT/'deliverables/ProductAutoML_V6_Offline_20261027.zip.json').read_text(encoding='utf-8'))
    for path in (ROOT/'deliverables').glob('ProductAutoML_V6*.zip'):
        if path.stat().st_size==meta['bytes'] and hashlib.sha256(path.read_bytes()).hexdigest()==meta['sha256']:return path
    raise ValueError('缺少已验收摘要一致的V6基础包（允许用户重命名ZIP）。')

def patch_entries():
    entries,_=previous_entries()
    for name in PATCH_FILES:
        if not name.endswith('.bat'):entries[name]=(ROOT/name).read_bytes()
    launchers=[('APPLY_V7.bat','upgrade_product_v7.py',''),('APPLY_V7_QUICK.bat','upgrade_product_v7.py','--profile quick'),
        ('APPLY_V7_DEEP.bat','upgrade_product_v7.py','--profile deep'),('SELECT_V7.bat','product_modes.py','select --engine optuna_v7'),
        ('COMPARE_V3_PLUS.bat','product_compare_v7.py','')]
    launchers += [('V7_'+m.upper()+'.bat','product_modes.py','select --engine optuna_v7 --strategy '+m) for m in ('balanced','goal_first','baseline_only')]
    launchers += [('V7_EXPERT_'+m.upper()+'.bat','product_modes.py','select --engine optuna_v7 --consistency '+m) for m in ('off','report','soft','strict')]
    for name,script,args in launchers:entries[name]=batch(script,args);(ROOT/name).write_bytes(entries[name])
    entries['V7使用说明.md']=entries['docs/OPTUNA_V7_PIPELINE.md']
    for name in list(entries):
        if name.startswith('OPTUNA_') and name.endswith('_PATCH_FILES.json'):entries.pop(name)
    dependency=validate_local_imports(entries,ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013',optional_modules={'tools.product_price_tabpfn'})
    entries['OPTUNA_V7_PATCH_FILES.json']=encoded({'format':'optuna-v7-patch-1.0','dependency_check':dependency,
        'files':[{'path':n,'bytes':len(b),'sha256':hashlib.sha256(b).hexdigest()} for n,b in sorted(entries.items())]})
    return entries,dependency

def build(output,full=False,evidence=None):
    target=Path(output).resolve()
    if ROOT/'deliverables' not in target.parents or target.exists():raise ValueError('请选择新的deliverables ZIP。')
    entries,dependency=patch_entries();prefix='ProductAutoML_V7';immutable=[]
    if full:
        with zipfile.ZipFile(base_archive()) as old:
            start=old.namelist()[0].split('/')[0]+'/';settings=json.loads(old.read(start+'config/portable.json').decode('utf-8'));registry=json.loads(old.read(start+'state/modes.json').decode('utf-8'))
        sample=(ROOT/'automl_pipeline/examples/virtual_v7_16x36.xlsx').read_bytes()
        settings.update(price_engine='optuna_v7',product_name='成品价格效能与推荐（V7/V3+虚拟演示）',demo_input_sha256=hashlib.sha256(sample).hexdigest())
        entries['config/portable.json']=encoded(settings);entries['config/price_optuna_v7.json']=encoded(load_config());entries['input/products.xlsx']=sample
        for mode in ('balanced','goal_first','baseline_only'):
            demo=Path(evidence)/('demo_v7_'+mode);manifest=json.loads((demo/'prototype_manifest.json').read_text(encoding='utf-8'))
            if manifest.get('status')!='ready' or manifest.get('price_engine')!='optuna_v7' or not manifest.get('virtual_data'):raise ValueError('未完成V7虚拟演示：'+mode)
            relative='outputs/demo_v7_'+mode;cfg=json.loads((demo/'price/effective_config.json').read_text(encoding='utf-8'))
            for path in demo.rglob('*'):
                rel=path.relative_to(demo)
                if not path.is_file() or set(rel.parts)&{'__pycache__','input','backups','checkpoints','transient_search'} or path.suffix in ('.pyc','.pyo','.log') or path.name=='running.json':continue
                # Full native V3 baseline and search reports are retained for the comparison command.
                entries[relative+'/'+rel.as_posix()]=path.read_bytes()
            desc=descriptor('optuna_v7',cfg);key=mode_key(desc)
            pointer={'format':'product-portable-latest-1.0','status':'ready','output':relative,'price_engine':'optuna_v7','mode':desc,'mode_key':key,'input_sha256':settings['demo_input_sha256']}
            registry['models'][key]=pointer
            if mode=='balanced':entries['state/latest.json']=encoded(pointer);entries['state/latest_optuna_v7.json']=encoded(pointer)
        entries['state/modes.json']=encoded(registry)
        entries['使用说明.txt']=('V7/V3+：原V3六模型联合搜索，新增小规模开发复核、训练种子平均和收缩融合。\n'
            'START直接运行保存的虚拟演示，不训练。换input/products.xlsx后CHECK_INPUT、RETRAIN；EXPORT导出模型和环境。\n'
            '专家1—5分五档错分率默认关闭，可选report/soft/strict，默认阈值10%。COMPARE_V3_PLUS读取同口径原V3对照。\n'
            '所有开发分数参与选择，不是独立测试成绩。更难虚拟演示可能未达标，前台和报告如实标记。\n'
            'V2—V6原模式仍可切换，V5默认5.1；V21.6门户、效费分析台和四端口退出保留。详见V7使用说明.md。\n').encode('utf-8-sig')
    def write(z,name,data):
        z.writestr((prefix+'/' if full else '')+name,data)
        if full and (name.split('/')[0] in ('runtime','app','services','tools','automl_pipeline','docs','cost_effectiveness_analysis') or name.endswith('.bat')):
            immutable.append({'path':name,'size':len(data),'sha256':hashlib.sha256(data).hexdigest()})
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED,compresslevel=6,allowZip64=True) as z:
        if full:
            with zipfile.ZipFile(base_archive()) as old:
                for name in old.namelist():
                    relative='/'.join(name.split('/')[1:])
                    if relative and relative not in entries and relative!='PACKAGE_MANIFEST.json':write(z,relative,old.read(name))
        for name,data in entries.items():write(z,name,data)
        if full:z.writestr(prefix+'/PACKAGE_MANIFEST.json',encoded({'format':'product-offline-manifest-1.0','training_revision':'Optuna_V7_V3plus_1.0','recommendation_version':'V21.6','immutable_files':sorted(immutable,key=lambda r:r['path'])}))
    with zipfile.ZipFile(target) as z:assert z.testzip() is None
    result={'status':'BUILT_PENDING_VERIFICATION','path':str(target),'bytes':target.stat().st_size,'sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
        'embedded_environment':full,'training_revision':'V7-V3plus','recommendation_version':'V21.6','dependency_check':dependency,'business_accuracy_certified':False}
    Path(str(target)+'.json').write_bytes(encoded(result));Path(str(target)+'.sha256').write_text(result['sha256']+'  '+target.name+'\n',encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False));return result

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--full',action='store_true');p.add_argument('--evidence',default=str(ROOT/'outputs/v7_20261030/final_demos'));a=p.parse_args();build(a.output,a.full,a.evidence)
if __name__=='__main__':main()
