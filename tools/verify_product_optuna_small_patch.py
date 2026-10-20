# -*- coding: utf-8 -*-
"""Latest dual-version offline package + small-sample PATCH -> actual 4-7 row BAT training."""
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


def main():
    p=argparse.ArgumentParser();p.add_argument('--zip',required=True);p.add_argument('--destination',required=True);p.add_argument('--report',required=True);p.add_argument('--metric-failure-case',action='store_true');a=p.parse_args()
    root=Path(a.destination).resolve();report=Path(a.report).resolve()
    if ROOT/'outputs' not in root.parents or root.exists():raise ValueError('Choose a new outputs directory')
    original=ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013/runtime'
    root.mkdir();shutil.copytree(str(original),str(root/'runtime'),copy_function=os.link,ignore=shutil.ignore_patterns('__pycache__'))
    with zipfile.ZipFile(str(ROOT/'deliverables/ProductAutoML_V2_V3_Offline_20261016.zip')) as archive:
        for name in archive.namelist():
            relative='/'.join(name.split('/')[1:])
            if not relative or relative.startswith('runtime/'):continue
            path=root/relative;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(archive.read(name))
    def model_snapshot():
        result={}
        for engine in ('optuna_v2','optuna_v3'):
            pointer=json.loads((root/'state'/('latest_'+engine+'.json')).read_text(encoding='utf-8'));output=root/pointer['output']
            result[engine]=(sha(output/'price/price_native_bundle.pkl'),(output/'price/price_native_bundle.pkl').stat().st_mtime_ns)
        return result
    old_models=model_snapshot()
    with zipfile.ZipFile(a.zip) as archive:
        payload=json.loads(archive.read('OPTUNA_DUAL_PATCH_FILES.json').decode('utf-8'))
        for item in payload['files']:assert hashlib.sha256(archive.read(item['path'])).hexdigest()==item['sha256']
        archive.extractall(str(root))
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='');python=root/'runtime/python38/python.exe'
    def run(args,label,success=True,timeout=240):
        result=subprocess.run(args,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=result.stdout.decode('utf-8',errors='replace');report.with_name(label+'.log').write_text(text,encoding='utf-8')
        if success and result.returncode:raise AssertionError(label+': '+text)
        if not success:assert result.returncode!=0
        return text
    def bat(name,label,*args,**kwargs):return run(['cmd.exe','/d','/c',name]+list(args),label,**kwargs)
    bat('APPLY_V2_V3.bat','01_apply');assert old_models==model_snapshot()
    run([str(python),'-X','utf8','tools/product_portable.py','verify','--full'],'02_manifest')
    configs={}
    for short in ('v2','v3'):
        path=root/('config/price_optuna_'+short+'.json');cfg=json.loads(path.read_text(encoding='utf-8'))
        assert cfg['input']['min_samples']==4 and cfg['validation']['auto_adjust_test_size'] is True
        cfg['search']['n_trials']=2;cfg['expert']['scores']={'x0':5,'x4':4};cfg['expert']['field_types']={'x4':'配置类型'}
        if short=='v3':cfg['v3']['startup_trials']=1
        path.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8');configs[short]=cfg
    (root/'input/products.xlsx').rename(root/'input/products.xlsx.disabled');cases=[];metric_case=None
    if a.metric_failure_case:
        rows=[['成品编号','成品名称','价格(万元)','价格属性0','价格属性1'],['product_id','product_name','price_wan','x0','x1']]
        rows.extend(['E%d'%i,'有限数据外推验证',y,x,0.] for i,(x,y) in enumerate(zip((0.,1e6,1e-6,2e6),(100.,200.,300.,400.))))
        stream=io.StringIO(newline='');csv.writer(stream).writerows(rows);source=root/'input/products.csv';source.write_bytes(stream.getvalue().encode('utf-8-sig'))
        cfg=json.loads(json.dumps(configs['v3']));cfg['search']['n_trials']=10;cfg['expert']['scores']={};cfg['expert']['field_types']={};cfg['v3']['warm_start']['auto_history']=False
        (root/'config/price_optuna_v3.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
        bat('SELECT_V3.bat','metric_01_select');bat('CHECK_INPUT.bat','metric_02_inspect');bat('RETRAIN.bat','metric_03_actual_retrain')
        pointer=json.loads((root/'state/latest.json').read_text(encoding='utf-8'));out=root/pointer['output'];r=json.loads((out/'price/training_report.json').read_text(encoding='utf-8'))
        diagnostic=json.loads((out/'price/v3_diagnostics.json').read_text(encoding='utf-8'))
        assert r['failed_trials']>=1 and r['trials_completed']==10 and diagnostic['valid_trials']>0
        assert any(f['diagnostic'].get('kind')=='nonfinite_metric' for f in diagnostic['failures'])
        metric_case={'records':4,'features':2,'failed_candidates':r['failed_trials'],'valid_candidates':diagnostic['valid_trials'],'finite_input_with_positive_prices':True,
                     'export_completed':True,'diagnostics_saved':True,'saved_output':pointer['output']}
        (root/'config/price_optuna_v3.json').write_text(json.dumps(configs['v3'],ensure_ascii=False,indent=2),encoding='utf-8')
    for n in range(4,8):
        data=mixed_fixture(n,40);specs=data['parameters'];stream=io.StringIO(newline='');writer=csv.writer(stream)
        writer.writerow(['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs]);writer.writerow(['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs])
        for row in data['agreements']:writer.writerow([row['agreement_id'],'小样本成品',row['historical_price_wan']]+[row['params'][p['parameter_id']] for p in specs])
        source=root/'input/products.csv';source.write_bytes(stream.getvalue().encode('utf-8-sig'))
        settings_path=root/'config/portable.json';settings=json.loads(settings_path.read_text(encoding='utf-8'));settings['demo_input_sha256']=sha(source);settings_path.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
        for short in ('v2','v3'):
            label='%d_%s'%(n,short);bat('SELECT_'+short.upper()+'.bat',label+'_select')
            if n==4 and short=='v2':
                cfgpath=root/'config/price_optuna_v2.json';bad=dict(configs['v2'],input=dict(configs['v2']['input'],min_samples=6))
                cfgpath.write_text(json.dumps(bad,ensure_ascii=False,indent=2),encoding='utf-8')
                before=(root/'state/latest.json').read_bytes();failed=bat('CHECK_INPUT.bat','03_minimum_guard',success=False)
                assert '配置的6条' in failed and (root/'state/latest.json').read_bytes()==before
                cfgpath.write_text(json.dumps(configs['v2'],ensure_ascii=False,indent=2),encoding='utf-8')
            bat('CHECK_INPUT.bat',label+'_inspect');bat('RETRAIN.bat',label+'_retrain')
            pointer=json.loads((root/'state/latest.json').read_text(encoding='utf-8'));output=root/pointer['output'];trained=json.loads((output/'price/training_report.json').read_text(encoding='utf-8'))
            assert trained['training_rows']==n and trained['feature_count']==40 and trained['validation_train_rows']==n-2 and trained['validation_test_rows']==2
            assert trained['min_samples']==4 and trained['tiny_sample_mode'] and trained['selected_split_reload_verified'] and trained['native_equivalence_max_delta_wan']<1e-5
            assert trained['validation_split']['adjusted']==(n<6)
            cases.append({'engine':trained['engine'],'records':n,'features':40,'train':n-2,'validation':2,'adjusted':n<6,'saved_output':pointer['output']})
            if n==4 and short=='v3':
                exercise(root,report.with_name('04_four_record_start.log'),generation=True,expected_rows=4,expected_features=40,change_field='x1',change_delta=1.)
    # No training input is needed to start or export a fitted tiny-sample product.
    (root/'input/products.csv').rename(root/'input/products.csv.disabled')
    final_start=exercise(root,report.with_name('05_seven_record_start.log'),generation=False,expected_rows=7,expected_features=40,change_field='x1',change_delta=1.)
    export=root.parent/'small_saved_test.zip';unpack=root.parent/'small_x'
    if export.exists() or unpack.exists():raise ValueError('Use clean verification export paths')
    bat('EXPORT.bat','06_export','--output',str(export));unpack.mkdir()
    with zipfile.ZipFile(str(export)) as archive:
        assert archive.testzip() is None and not any('/input/' in name for name in archive.namelist())
        archive.extractall(str(unpack));folder=archive.namelist()[0].split('/')[0]
    relocated=exercise(unpack/folder,report.with_name('07_relocated_saved_start.log'),generation=False,expected_rows=7,expected_features=40,change_field='x1',change_delta=1.)
    result={'status':'PASS','patch_sha256':sha(a.zip),'old_models_preserved_by_installation':True,'minimum_config_saved_both_engines':4,
        'configured_higher_minimum_rejects_before_training':True,'actual_check_input_and_retrain':cases,'four_record_recommendation_generation':True,
        'seven_record_saved_start_without_input':final_start,'relocated_export_without_input':relocated,'real_nonfinite_candidate_recovery':metric_case,'business_accuracy_certified':False}
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'status':'PASS','cases':len(cases),'patch_sha256':result['patch_sha256']},ensure_ascii=False))
if __name__=='__main__':main()
