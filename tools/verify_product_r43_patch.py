# -*- coding: utf-8 -*-
"""Exercise the actual ZIP overlay, saved START and the new price-risk API."""
import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.verify_product_r4_portable import exercise


def main():
    p=argparse.ArgumentParser();p.add_argument('--zip',required=True);p.add_argument('--destination',required=True);p.add_argument('--report',required=True);p.add_argument('--reuse',action='store_true');a=p.parse_args()
    source=ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013';dest=Path(a.destination).resolve()
    if ROOT not in dest.parents or (dest.exists() and not a.reuse):raise ValueError('Use a new workspace verification directory')
    def copier(src,dst):
        if Path(src).relative_to(source).parts[0]=='runtime':os.link(str(src),str(dst));return dst
        return shutil.copy2(src,dst)
    if not dest.exists():shutil.copytree(str(source),str(dest),copy_function=copier,ignore=shutil.ignore_patterns('__pycache__','cache','oof_cache','exports'))
    with zipfile.ZipFile(a.zip) as z:
        assert z.testzip() is None
        for name in z.namelist():
            if dest not in (dest/name).resolve().parents:raise ValueError('Unsafe ZIP path')
        payload=json.loads(z.read('R43_PATCH_FILES.json').decode('utf-8'))
        for item in payload['files']:assert hashlib.sha256(z.read(item['path'])).hexdigest()==item['sha256']
        z.extractall(str(dest))
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='');python=dest/'runtime/python38/python.exe'
    for command in (['cmd.exe','/d','/c','APPLY_R4_3_QUICK.bat'],[str(python),'-X','utf8','tools/product_portable.py','verify','--full']):
        result=subprocess.run(command,cwd=str(dest),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90)
        if result.returncode:raise AssertionError(result.stdout.decode('utf-8',errors='replace'))
    report=Path(a.report).resolve();report.parent.mkdir(parents=True,exist_ok=True)
    result=exercise(dest,report.with_suffix('.old_start.log'),generation=False)
    result.update(zip_payload_hashes=True,zip_overlay_and_full_manifest=True,runtime_files_hardlinked_for_test_only=True)
    directory=ROOT/'outputs/r43_validation_20261015/quadratic_noisy';bundle=directory/'training/price_native_bundle.pkl'
    data=json.loads((directory/'fixed_training_records.json').read_text(encoding='utf-8'));parameters=data['agreements'][0]['params']
    with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
    log=report.with_suffix('.new_price.log')
    with log.open('w',encoding='utf-8') as handle:
        child=subprocess.Popen([str(python),'-u','-m','services.price_service.app','--model',str(bundle),'--port',str(port)],cwd=str(dest),env=env,stdout=handle,stderr=subprocess.STDOUT,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}));deadline=time.monotonic()+45
        def post(payload):
            request=urllib.request.Request('http://127.0.0.1:%d/api/v1/predict'%port,data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'})
            with opener.open(request,timeout=15) as r:return json.loads(r.read().decode('utf-8'))
        try:
            while time.monotonic()<deadline:
                if child.poll() is not None:raise AssertionError(log.read_text(encoding='utf-8'))
                try:
                    with opener.open('http://127.0.0.1:%d/health'%port,timeout=2) as r:
                        if json.loads(r.read().decode('utf-8')).get('status')=='ok':break
                except Exception:time.sleep(.2)
            else:raise AssertionError('New model startup timeout')
            normal=post({'parameters':parameters});assert normal.get('success') is True,normal
            prediction=normal['prediction'];assert 'price_risk' in prediction
            assert prediction['recommendation_price_wan']>=prediction['predicted_price_wan']
            extreme=post({'parameters':{key:20. for key in parameters}});assert extreme.get('success') is True,extreme
            assert extreme['prediction']['price_risk']['is_extrapolation'] is True
            assert extreme['domain_status']['in_domain'] is False
            assert extreme['prediction']['recommendation_price_wan']>=extreme['prediction']['predicted_price_wan']
            result.update(new_model_http_prediction=normal,new_model_extrapolation_http=extreme,new_model_loaded_from_delivered_code=True)
        finally:child.terminate();child.wait(timeout=15)
    result['zip_sha256']=hashlib.sha256(Path(a.zip).read_bytes()).hexdigest()
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'PASS','old_start_no_retraining':result['no_retraining'],'new_model_and_risk_http':True,'full_manifest':True},ensure_ascii=False))
if __name__=='__main__':main()
