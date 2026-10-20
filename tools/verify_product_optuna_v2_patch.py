# -*- coding: utf-8 -*-
"""Isolated ZIP -> Excel -> RETRAIN -> three services -> exported offline runtime."""
import argparse
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v2_test import fixture,workbook
from tools.verify_product_r4_portable import exercise as exercise_old
from services.price_service.optuna_v2 import price_automl_core as core
import pandas as pd


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--zip',required=True);p.add_argument('--destination',required=True);p.add_argument('--export',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    source=ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013';dest=Path(a.destination).resolve();report=Path(a.report).resolve();export=Path(a.export).resolve()
    if ROOT/'outputs' not in dest.parents or dest.exists() or export.exists():raise ValueError('Use new verification paths')
    report.parent.mkdir(parents=True,exist_ok=True)
    def copier(src,dst):
        if Path(src).relative_to(source).parts[0]=='runtime':os.link(str(src),str(dst));return dst
        return shutil.copy2(src,dst)
    shutil.copytree(str(source),str(dest),copy_function=copier,ignore=shutil.ignore_patterns('__pycache__','cache','oof_cache','exports'))
    with zipfile.ZipFile(a.zip) as z:
        assert z.testzip() is None;payload=json.loads(z.read('OPTUNA_V2_PATCH_FILES.json').decode('utf-8'))
        for item in payload['files']:assert hashlib.sha256(z.read(item['path'])).hexdigest()==item['sha256']
        for name in z.namelist():
            if dest not in (dest/name).resolve().parents:raise ValueError('Unsafe ZIP path')
        z.extractall(str(dest))
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='');python=dest/'runtime/python38/python.exe'
    def run(args,label,timeout=180):
        r=subprocess.run(args,cwd=str(dest),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=r.stdout.decode('utf-8',errors='replace');report.with_name(label+'.log').write_text(text,encoding='utf-8')
        if r.returncode:raise AssertionError(label+': '+text)
        return text
    before_state=(dest/'state/latest.json').read_bytes()
    run(['cmd.exe','/d','/c','APPLY_OPTUNA_V2_QUICK.bat'],'01_apply')
    assert before_state==(dest/'state/latest.json').read_bytes()
    run([str(python),'-X','utf8','tools/product_portable.py','verify','--full'],'02_full_manifest')
    old=exercise_old(dest,report.with_name('03_old_saved_start.log'),generation=False);assert old['no_retraining']
    X,y,specs,data=fixture();raw=workbook(data);(dest/'input/products.xlsx').write_bytes(raw)
    settings_path=dest/'config/portable.json';settings=json.loads(settings_path.read_text(encoding='utf-8'));settings.update(product_name='联合搜索虚拟成品演示',demo_input_sha256=hashlib.sha256(raw).hexdigest())
    settings_path.write_text(json.dumps(settings,ensure_ascii=False,indent=2),encoding='utf-8')
    cfgpath=dest/'config/price_optuna_v2.json';cfg=json.loads(cfgpath.read_text(encoding='utf-8'));cfg['search']['n_trials']=7;cfgpath.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    run(['cmd.exe','/d','/c','CHECK_INPUT.bat'],'04_excel_inspect')
    run(['cmd.exe','/d','/c','RETRAIN.bat'],'05_actual_retrain',timeout=240)
    state=(dest/'state/latest.json').read_bytes();output=dest/json.loads(state)['output'];manifest=json.loads((output/'prototype_manifest.json').read_text(encoding='utf-8'))
    assert manifest['price_engine']=='optuna_v2' and manifest['virtual_data'] is True
    r=json.loads((output/'price/training_report.json').read_text(encoding='utf-8'))
    assert r['training_rows']==16 and r['feature_count']==36 and r['trials_completed']==7 and r['business_accuracy_certified'] is False
    assert r['native_equivalence_max_delta_wan']<1e-5
    model=core.load_bundle(str(output/'price/price_ensemble_bundle.pkl'));reference=float(core.predict_bundle(model,pd.DataFrame([data['agreements'][0]['params']]))[0][0])
    hashes={key:(sha(output/manifest[key]),(output/manifest[key]).stat().st_mtime_ns) for key in ('price_model','effectiveness_model','business_package')}
    # After training, neither saved START nor EXPORT may need the input file.
    (dest/'input/products.xlsx').rename(dest/'input/verification_original.xlsx.disabled')
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(url):
        with opener.open(url,timeout=15) as response:return json.loads(response.read().decode('utf-8'))
    def post(url,payload):
        request=urllib.request.Request(url,data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=45) as response:return json.loads(response.read().decode('utf-8'))
    def exercise(package,log_name,generation=False):
        latest=(package/'state/latest.json').read_bytes();version=package/json.loads(latest)['output'];began=time.time();runtime=None
        with report.with_name(log_name+'.log').open('w',encoding='utf-8') as log:
            child=subprocess.Popen(['cmd.exe','/d','/c','START.bat','--no-browser','--price-port','0','--effect-port','0','--app-port','0'],cwd=str(package),env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    if child.poll() is not None:raise AssertionError('START failed; see '+log_name)
                    try:
                        path=version/'running.json'
                        if path.stat().st_mtime>=began:
                            runtime=json.loads(path.read_text(encoding='utf-8'));boot=get(runtime['url'].rstrip('/')+'/api/bootstrap')
                            if boot['counts']['historical']==16 and boot['integration']['product_ready']:break
                    except Exception:time.sleep(.2)
                else:raise AssertionError('START timeout')
                base=runtime['url'].rstrip('/');assert len(boot['parameters'])==36
                schema=get(base+'/api/price-workbench/schema');assert len(schema['fields'])==36 and all('价格属性' in f['field_label'] for f in schema['fields'])
                prediction=post(base+'/api/price-workbench/predict',{'parameters':data['agreements'][0]['params']})
                assert abs(prediction['prediction']['predicted_price_wan']-reference)<1e-5
                assert len(prediction['explanation']['price_attribute_scores'])==36
                assert abs(sum(v['weighted_contribution_wan'] for v in prediction['explanation']['model_contributions'])-reference)<1e-5
                evaluation=post(base+'/api/evaluate',{'parameters':data['agreements'][0]['params']});assert evaluation['predicted_price_wan']>0
                query={'session_id':'optuna-v2-acceptance','source_mode':'historical','selected_tags':[],'indicator_filters':[],'sort_by':'comprehensive','count':1,'generation_count':1,'page':1,'page_size':20,'start_generation':False}
                ranking=post(base+'/api/recommend',query);assert ranking['total']>0
                if generation:
                    task=post(base+'/api/generation/request',query);deadline=time.monotonic()+90
                    while task['status'] not in ('completed','failed') and time.monotonic()<deadline:
                        time.sleep(.2);task=get(base+'/api/generation-tasks/'+task['task_id'])
                    assert task['status']=='completed',task
                    generated=post(base+'/api/recommend',dict(query,source_mode='generated',generation_batch_id=task['batch_id']));assert generated['total']>0
                return {'prediction':prediction['prediction']['predicted_price_wan'],'historical_recommendations':ranking['total'],'generation_verified':generation,'no_input_table_required':True}
            finally:
                if child.poll() is None:
                    child.send_signal(signal.CTRL_BREAK_EVENT)
                    try:child.wait(timeout=15)
                    except subprocess.TimeoutExpired:
                        subprocess.run(['taskkill.exe','/PID',str(child.pid),'/T','/F'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT);child.wait(timeout=10)
                assert (package/'state/latest.json').read_bytes()==latest
    before_export=exercise(dest,'06_new_model_saved_start',generation=True)
    assert state==(dest/'state/latest.json').read_bytes()
    assert hashes=={key:(sha(output/manifest[key]),(output/manifest[key]).stat().st_mtime_ns) for key in hashes}
    run(['cmd.exe','/d','/c','EXPORT.bat','--output',str(export)],'07_actual_export',timeout=240)
    assert state==(dest/'state/latest.json').read_bytes()
    assert hashes=={key:(sha(output/manifest[key]),(output/manifest[key]).stat().st_mtime_ns) for key in hashes}
    unpack=dest.parent/(dest.name+'_x');unpack.mkdir()
    with zipfile.ZipFile(str(export)) as z:
        assert z.testzip() is None
        assert not any('/input/' in name or '/backups/' in name for name in z.namelist())
        root_name=z.namelist()[0].split('/')[0];z.extractall(str(unpack))
    saved_package=unpack/root_name
    assert (saved_package/'runtime/python38/python.exe').is_file() and not (saved_package/'input/products.xlsx').exists()
    exported=exercise(saved_package,'08_relocated_exported_start')
    result={'status':'PASS','old_saved_version_no_retraining':True,'zip_manifest_and_local_dependencies':True,'bilingual_excel_16x36_retrain':True,
        'original_core_native_http_predictions_match':True,'price_effect_recommendation_and_generation':before_export,'new_saved_start_no_training':True,
        'actual_export_bat_with_embedded_environment':True,'exported_zip_relocated_start_without_input':exported,
        'export_did_not_change_model_hashes_or_mtimes':True,'patch_sha256':sha(a.zip),'saved_zip':str(export),'saved_zip_sha256':sha(export),
        'trials':r['trials_completed'],'deployment_scope':r['deployment_scope'],'business_accuracy_certified':False}
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
