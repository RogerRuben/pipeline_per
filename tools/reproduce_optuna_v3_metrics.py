# -*- coding: utf-8 -*-
"""Exercise duplicate-price folds and full-length V3 phase transitions."""
import copy
import argparse
import json
import sys
import traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
import pandas as pd
from product_optuna_v3_test import mixed_fixture
from tools.product_price_optuna_v2 import train_price_optuna_v2


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--after',action='store_true');parser.add_argument('--normal-only',action='store_true');args=parser.parse_args()
    output=ROOT/'outputs/optuna_v3_metric_fix_20261016';output.mkdir(parents=True,exist_ok=True);results=[]
    fixtures=[('duplicate7',7,24),('duplicate16',16,40),('near_constant7',7,30),('normal16_300',16,300)]
    if args.normal_only:fixtures=fixtures[-1:]
    for name,n,budget in fixtures:
        data=mixed_fixture(n,40)
        if name.startswith('duplicate'):
            for i,row in enumerate(data['agreements']):row['historical_price_wan']=100. if i<n-1 else 140.
        if name.startswith('near_constant'):
            for i,row in enumerate(data['agreements']):row['historical_price_wan']=100. if i<n-2 else 100.+1e-9 if i==n-2 else 10000.
        for engine in ('optuna_v2','optuna_v3'):
            if name=='normal16_300' and engine=='optuna_v2':continue
            folder=output/(('after_' if args.after else '')+name+'_'+engine)
            if folder.exists():continue
            config={'search':{'n_trials':budget},'expert':{'field_types':{'x4':'配置类型'}}}
            if engine=='optuna_v3':config['v3']={'startup_trials':2,'warm_start':{'auto_history':False}}
            try:
                _,report=train_price_optuna_v2(data,folder,overrides=config,engine=engine,progress=lambda s:None)
                trials=pd.read_csv(folder/'optuna_trials.csv')
                result={'case':name,'engine':engine,'status':'PASS','trials':report['trials_completed'],'invalid_trials':int((~trials['valid']).sum()),'metrics':report['validation_metrics']}
            except Exception as exc:result={'case':name,'engine':engine,'status':'ERROR','error':repr(exc),'traceback':traceback.format_exc()}
            results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
            (output/('after_reproduction.json' if args.after else 'reproduction.json')).write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
    if args.after and args.normal_only:
        before=json.loads((output/'normal16_300_optuna_v3/best_config.json').read_text(encoding='utf-8'))
        after=json.loads((output/'after_normal16_300_optuna_v3/best_config.json').read_text(encoding='utf-8'))
        assert before['selected_parameters']==after['selected_parameters']
        assert abs(before['validation_metrics']['mape']-after['validation_metrics']['mape'])<1e-10
        (output/'normal_300_parity.json').write_text(json.dumps({'status':'PASS','trials':300,'selected_parameters_unchanged':True,'validation_metrics_unchanged':True},indent=2),encoding='utf-8')
if __name__=='__main__':main()
