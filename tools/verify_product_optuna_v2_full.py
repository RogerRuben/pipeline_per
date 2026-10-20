# -*- coding: utf-8 -*-
import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.verify_product_r4_portable import exercise


def main():
    p=argparse.ArgumentParser();p.add_argument('--zip',required=True);p.add_argument('--destination',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    dest=Path(a.destination).resolve();report=Path(a.report).resolve()
    if ROOT/'outputs' not in dest.parents or dest.exists():raise ValueError('Choose a new outputs directory')
    with zipfile.ZipFile(a.zip) as z:
        assert z.testzip() is None;z.extractall(str(dest))
    package=dest/'ProductOptunaV2';env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='');python=package/'runtime/python38/python.exe'
    def run(args,name):
        r=subprocess.run(args,cwd=str(package),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=180,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=r.stdout.decode('utf-8',errors='replace');report.with_name(name+'.log').write_text(text,encoding='utf-8')
        if r.returncode:raise AssertionError(text)
    run([str(python),'-X','utf8','tools/product_portable.py','verify','--full'],'full_01_manifest')
    run(['cmd.exe','/d','/c','CHECK_INPUT.bat'],'full_02_input')
    config=package/'config/price_optuna_v2.json';cfg=json.loads(config.read_text(encoding='utf-8'));assert cfg['search']['n_trials']==150 and not cfg['expert']['scores']
    initial=exercise(package,report.with_name('full_03_saved_demo.log'),generation=False,expected_rows=16,expected_features=36,change_field='x0',change_delta=.05)
    # Isolated acceptance uses two trials; distributed defaults remain 150.
    cfg['search']['n_trials']=2;config.write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    run(['cmd.exe','/d','/c','RETRAIN.bat'],'full_04_retrain')
    after=exercise(package,report.with_name('full_05_after_retrain.log'),generation=False,expected_rows=16,expected_features=36,change_field='x0',change_delta=.05)
    result={'status':'PASS','full_manifest':True,'embedded_python_environment':True,'default_trials':150,'dual_header_input_and_scores':True,'saved_demo_starts':initial,
        'actual_retrain_bat':True,'new_model_starts_without_retraining':after,'zip_sha256':hashlib.sha256(Path(a.zip).read_bytes()).hexdigest(),'business_accuracy_certified':False}
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'status':'PASS','trainable_offline_package':True},ensure_ascii=False))
if __name__=='__main__':main()
