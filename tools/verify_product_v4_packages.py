# -*- coding: utf-8 -*-
"""Exercise actual ZIPs/BATs in isolated directories, with no source import fallback."""
import argparse
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v3_test import mixed_fixture
from tools.verify_product_r4_portable import exercise


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def unpack(archive_path,root):
    if root.exists():raise ValueError('Verification destination already exists')
    root.mkdir();shutil.copytree(str(ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013/runtime'),str(root/'runtime'),copy_function=os.link,ignore=shutil.ignore_patterns('__pycache__'))
    with zipfile.ZipFile(str(archive_path)) as archive:
        assert archive.testzip() is None
        for name in archive.namelist():
            relative='/'.join(name.split('/')[1:])
            if not relative or relative.startswith('runtime/'):continue
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.read(name))


def main():
    p=argparse.ArgumentParser();p.add_argument('--full',required=True);p.add_argument('--patch',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    evidence=Path(a.report).resolve().parent;full=ROOT/'outputs/v4f';patch=ROOT/'outputs/v4p';unpack(a.full,full)
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='')
    def run(root,args,label,success=True):
        result=subprocess.run(args,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=300,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=result.stdout.decode('utf-8',errors='replace');(evidence/(label+'.log')).write_text(text,encoding='utf-8')
        if success and result.returncode:raise AssertionError(label+': '+text[-8000:])
        if not success and result.returncode==0:raise AssertionError(label+' unexpectedly succeeded')
        return text
    def bat(root,name,label,*args,**kwargs):return run(root,['cmd.exe','/d','/c',name]+list(args),label,**kwargs)
    def python(root,args,label,**kwargs):return run(root,[str(root/'runtime/python38/python.exe'),'-X','utf8']+args,label,**kwargs)
    python(full,['tools/product_portable.py','verify','--full'],'full_01_manifest')
    full_start=exercise(full,evidence/'full_02_start.log',generation=True,expected_rows=16,expected_features=36,change_field='x1',change_delta=1.)
    saved_switch=[]
    for name in ('SELECT_V2','SELECT_V3','V4_RANDOM','V4_GLOBAL','V4_PER_SPLIT','V4_HYBRID'):
        bat(full,name+'.bat','full_select_'+name)
        value=json.loads(python(full,['tools/product_modes.py','status'],'full_status_'+name))
        assert value['saved'] is not None;saved_switch.append({'selector':name,'saved_output':value['saved']['output']})
    # No input table is required for saved startup.
    (full/'input/products.xlsx').rename(full/'input/products.xlsx.disabled')
    full_no_input=exercise(full,evidence/'full_03_no_input.log',generation=False,expected_rows=16,expected_features=36,change_field='x1',change_delta=1.)
    unpack(ROOT/'deliverables/ProductAutoML_V2_V3_Offline_20261016.zip',patch)
    old=[]
    for name in ('v2','v3'):
        model=patch/('outputs/demo_'+name+'/price/price_native_bundle.pkl');old.append((sha(model),model.stat().st_mtime_ns))
    with zipfile.ZipFile(a.patch) as archive:
        manifest=json.loads(archive.read('OPTUNA_V4_PATCH_FILES.json').decode('utf-8'))
        for item in manifest['files']:assert hashlib.sha256(archive.read(item['path'])).hexdigest()==item['sha256']
        archive.extractall(str(patch))
    bat(patch,'APPLY_V4.bat','patch_01_apply')
    assert old==[(sha(patch/('outputs/demo_'+name+'/price/price_native_bundle.pkl')),(patch/('outputs/demo_'+name+'/price/price_native_bundle.pkl')).stat().st_mtime_ns) for name in ('v2','v3')]
    python(patch,['tools/product_portable.py','verify','--full'],'patch_02_manifest')
    bat(patch,'START.bat','patch_03_untrained_no_start','--no-browser',success=False)
    (patch/'input/products.xlsx').rename(patch/'input/products.xlsx.disabled')
    cfg_path=patch/'config/price_optuna_v4.json';cfg=json.loads(cfg_path.read_text(encoding='utf-8'));cfg['search']['n_trials']=36
    cfg['expert']['scores']={'x0':5,'x4':4};cfg['expert']['field_types']={p['parameter_id']:('enum' if p['parameter_id'] in ('x3','x4') else 'number') for p in mixed_fixture(7,40)['parameters']}
    cfg['v4']['review']['max_candidates']=3;cfg['v4']['ensemble']['weight_trials']=4
    cfg_path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    cases=[]
    def source(n):
        data=mixed_fixture(n,40);specs=data['parameters'];stream=io.StringIO(newline='');writer=csv.writer(stream)
        writer.writerow(['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs]);writer.writerow(['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs])
        for row in data['agreements']:writer.writerow([row['agreement_id'],'V4小样本成品',row['historical_price_wan']]+[row['params'][p['parameter_id']] for p in specs])
        path=patch/'input/products.csv';path.write_bytes(stream.getvalue().encode('utf-8-sig'))
        settings_path=patch/'config/portable.json';settings=json.loads(settings_path.read_text(encoding='utf-8'));settings['demo_input_sha256']=sha(path);settings_path.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    def train_case(n,mode,label):
        source(n);bat(patch,'V4_'+mode.upper()+'.bat',label+'_select');bat(patch,'CHECK_INPUT.bat',label+'_check');bat(patch,'RETRAIN.bat',label+'_train')
        pointer=json.loads((patch/'state/latest.json').read_text(encoding='utf-8'));report=json.loads((patch/pointer['output']/'price/training_report.json').read_text(encoding='utf-8'))
        assert report['training_rows']==n and report['feature_count']==40 and report['native_equivalence_max_delta_wan']<1e-5
        assert report['trials_completed']==36 and report['search_mode']==mode
        cases.append({'records':n,'mode':mode,'output':pointer['output'],'splits':report['split_plan']['evaluated_splits'],'experts':report['expert_scores_enabled'],'train_gate':report['train_gate']['mode'],'scope':report['deployment_scope']})
        return pointer,report
    for mode in ('random','global','per_split','hybrid'):train_case(4,mode,'patch_4_'+mode)
    for n in (5,6,7):train_case(n,'hybrid','patch_%d_hybrid'%n)
    source(7)
    python(patch,['tools/product_modes.py','select','--experts','off','--gate','off','--ensemble','off','--scope','selected_split'],'patch_pure_select')
    pointer,report=train_case(7,'global','patch_pure')
    assert not report['expert_scores_enabled'] and report['train_gate']['mode']=='off' and report['configuration_count']==1 and report['deployment_scope']=='selected_split'
    # Require targets: failing retrain must leave all old serving pointers intact.
    before=(patch/'state/latest.json').read_bytes();registry=(patch/'state/modes.json').read_bytes()
    strict=json.loads(cfg_path.read_text(encoding='utf-8'));strict['targets'].update(mape_percent_max=1e-12,r2_min=1.);strict['v4']['publish']['policy']='require_targets';strict['search']['n_trials']=2
    cfg_path.write_text(json.dumps(strict,ensure_ascii=False,indent=2),encoding='utf-8');bat(patch,'RETRAIN.bat','patch_strict_rejected',success=False)
    assert before==(patch/'state/latest.json').read_bytes() and registry==(patch/'state/modes.json').read_bytes()
    # Restore a trained/default semantic mode; data change is not needed for START.
    cfg_path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8');bat(patch,'V4_HYBRID.bat','patch_restore_hybrid')
    (patch/'input/products.csv').rename(patch/'input/products.csv.disabled')
    final_start=exercise(patch,evidence/'patch_saved_7_start.log',generation=True,expected_rows=7,expected_features=40,change_field='x1',change_delta=1.)
    export=ROOT/'outputs/v4_saved_test.zip';relocated=ROOT/'outputs/v4x'
    if export.exists() or relocated.exists():raise ValueError('Use clean export paths')
    bat(patch,'EXPORT.bat','patch_export','--output',str(export));relocated.mkdir()
    with zipfile.ZipFile(str(export)) as archive:
        assert archive.testzip() is None and not any('/input/' in name for name in archive.namelist());archive.extractall(str(relocated));folder=archive.namelist()[0].split('/')[0]
    relocated_start=exercise(relocated/folder,evidence/'patch_relocated_start.log',generation=False,expected_rows=7,expected_features=40,change_field='x1',change_delta=1.)
    result={'status':'PASS','full_sha256':sha(a.full),'patch_sha256':sha(a.patch),'full_saved_start':full_start,'full_saved_mode_switches':saved_switch,'full_start_without_input':full_no_input,
            'patch_preserves_old_models':True,'patch_untrained_mode_does_not_start_or_train':True,'actual_bat_training_cases':cases,'strict_retrain_preserves_serving_pointer':True,
            'saved_seven_record_start':final_start,'relocated_saved_export':relocated_start,'business_accuracy_certified':False}
    Path(a.report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'status':'PASS','cases':len(cases),'full_sha256':result['full_sha256'],'patch_sha256':result['patch_sha256']}))
if __name__=='__main__':main()
