# -*- coding: utf-8 -*-
"""Reproduce the ZIP failure and test real table import + RETRAIN in isolation."""
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
from product_price_r43_test import fixture,small_config
from app.xlsx_utils import write_workbook_bytes


def digest(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--zip',required=True);p.add_argument('--destination',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    source=ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013';dest=Path(a.destination).resolve()
    if ROOT/'outputs' not in dest.parents or dest.exists():raise ValueError('Choose a new outputs verification directory')
    def copier(src,dst):
        if Path(src).relative_to(source).parts[0]=='runtime':os.link(str(src),str(dst));return dst
        return shutil.copy2(src,dst)
    shutil.copytree(str(source),str(dest),copy_function=copier,ignore=shutil.ignore_patterns('__pycache__','cache','oof_cache','exports'))
    report=Path(a.report).resolve();report.parent.mkdir(parents=True,exist_ok=True)
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='');python=dest/'runtime/python38/python.exe'
    def run(args,name,expected=0,timeout=180):
        result=subprocess.run(args,cwd=str(dest),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        output=result.stdout.decode('utf-8',errors='replace');report.with_name(name+'.log').write_text(output,encoding='utf-8')
        if result.returncode!=expected:raise AssertionError(name+': '+output)
        return output
    def overlay(archive):
        with zipfile.ZipFile(archive) as z:
            assert z.testzip() is None
            for n in z.namelist():
                if dest not in (dest/n).resolve().parents:raise ValueError('Unsafe ZIP path')
            z.extractall(str(dest))
    def batch(name,log,expected=0):return run(['cmd.exe','/d','/c',name],log,expected)
    # The old failure must be observable without access to the source tree.
    overlay(ROOT/'deliverables/ProductAutoML_R4_3_PATCH_20261015.zip')
    batch('APPLY_R4_3_QUICK.bat','01_original_apply')
    output=batch('CHECK_INPUT.bat','02_original_check_input',1)
    assert 'tools.price_attribute_input' in output
    before={relative:(dest/relative).read_bytes() for relative in ('config/price_automl_r4.json','input/products.xlsx','state/latest.json')}
    latest=json.loads(before['state/latest.json']);old_output=dest/latest['output'];old_manifest=json.loads((old_output/'prototype_manifest.json').read_text(encoding='utf-8'))
    old_models={key:(digest(old_output/old_manifest[key]),(old_output/old_manifest[key]).stat().st_mtime_ns) for key in ('price_model','effectiveness_model','business_package')}
    overlay(a.zip);batch('APPLY_R4_3_INPUT_FIX.bat','03_repair')
    for relative,data in before.items():assert (dest/relative).read_bytes()==data,relative
    run([str(python),'-X','utf8','tools/product_portable.py','verify','--full'],'04_full_manifest')
    batch('CHECK_INPUT.bat','05_original_workbook_check')
    module_source=run([str(python),'-c',"from pathlib import Path;import sys;import tools.price_attribute_input as m;assert Path.cwd() in Path(m.__file__).resolve().parents;assert not any(p=="+repr(str(ROOT))+" for p in sys.path);print(m.__file__)"],'06_isolated_module')
    X,y,specs,data=fixture();rows=[['成品编号','成品名称','价格(万元)']+[s['label'] for s in specs],['product_id','product_name','price_wan']+[s['parameter_id'] for s in specs]]
    rows.extend([r['agreement_id'],'虚拟成品%d'%i,float(y[i])]+[float(v) for v in X[i]] for i,r in enumerate(data['agreements']))
    scores=[['英文代号','中文名','价格重要性评分'],['x0','属性0',5],['x1','属性1',1],['x35','属性35',3]]
    (dest/'input/products.xlsx').write_bytes(write_workbook_bytes([('成品数据',rows),('价格属性评分',scores)]))
    # Bounded software validation policy only; the repair preserved the user's
    # policy above, and this isolated copy now receives the test policy.
    (dest/'config/price_automl_r4.json').write_text(json.dumps(small_config(),ensure_ascii=False,indent=2),encoding='utf-8')
    batch('CHECK_INPUT.bat','07_new_16x36_workbook_check')
    run(['cmd.exe','/d','/c','RETRAIN.bat'],'08_actual_retrain',timeout=180)
    latest=json.loads((dest/'state/latest.json').read_text(encoding='utf-8'));trained=dest/latest['output']
    trained_report=json.loads((trained/'price/training_report.json').read_text(encoding='utf-8'))
    assert trained_report['training_rows']==16 and trained_report['feature_count']==36
    ratings=json.loads((trained/'price/price_attribute_scores.json').read_text(encoding='utf-8'))['attributes']
    ratings={r['field_id']:r['score'] for r in ratings};assert ratings['x0']==5 and ratings['x1']==1 and ratings['x35']==3
    manifest=json.loads((trained/'prototype_manifest.json').read_text(encoding='utf-8'))
    for key in ('price_model','effectiveness_model','business_package'):assert (trained/manifest[key]).is_file()
    for key,signature in old_models.items():assert (digest(old_output/old_manifest[key]),(old_output/old_manifest[key]).stat().st_mtime_ns)==signature
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(url):
        with opener.open(url,timeout=10) as r:return json.loads(r.read().decode('utf-8'))
    def post(url,payload):
        request=urllib.request.Request(url,data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'})
        with opener.open(request,timeout=20) as r:return json.loads(r.read().decode('utf-8'))
    saved_state=(dest/'state/latest.json').read_bytes();saved_model=digest(trained/manifest['price_model']);start=time.time()
    with report.with_suffix('.start.log').open('w',encoding='utf-8') as log:
        child=subprocess.Popen(['cmd.exe','/d','/c','START.bat','--no-browser','--price-port','0','--effect-port','0','--app-port','0'],cwd=str(dest),env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        try:
            deadline=time.monotonic()+45
            while time.monotonic()<deadline:
                if child.poll() is not None:raise AssertionError('Saved START failed; see '+str(report.with_suffix('.start.log')))
                try:
                    running_path=trained/'running.json'
                    if running_path.stat().st_mtime>=start:
                        running=json.loads(running_path.read_text(encoding='utf-8'));boot=get(running['url'].rstrip('/')+'/api/bootstrap')
                        if boot['counts']['historical']==16:break
                except Exception:time.sleep(.2)
            else:raise AssertionError('START timeout')
            assert len(boot['parameters'])==36 and boot['integration']['product_ready']
            prediction=post(running['url'].rstrip('/')+'/api/price-workbench/predict',{'parameters':data['agreements'][0]['params']})
            assert prediction['prediction']['predicted_price_wan']>0
        finally:
            if child.poll() is None:
                child.send_signal(signal.CTRL_BREAK_EVENT)
                try:child.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    subprocess.run(['taskkill.exe','/PID',str(child.pid),'/T','/F'],stdout=subprocess.PIPE,stderr=subprocess.STDOUT);child.wait(timeout=10)
    assert (dest/'state/latest.json').read_bytes()==saved_state and digest(trained/manifest['price_model'])==saved_model
    result={'status':'PASS','original_missing_module_reproduced':True,'repair_preserved_config_input_latest_and_old_models':True,'full_manifest_verified':True,
            'original_10x50_workbook_check':True,'new_16x36_workbook_check':True,'actual_retrain_bat_from_excel':True,'scores_1_to_5_imported':True,
            'price_effect_recommendation_artifacts':True,'fresh_saved_start_16x36_and_http_prediction':True,'saved_start_no_retraining':True,
            'no_repository_source_fallback':True,'module_loaded_from':module_source.strip(),'zip_sha256':digest(a.zip),
            'training_seconds':trained_report['elapsed_seconds'],'business_accuracy_certified':False}
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result,ensure_ascii=False))
if __name__=='__main__':main()
