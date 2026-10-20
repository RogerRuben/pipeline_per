# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
import numpy as np
from product_optuna_v2_test import fixture
from tools.product_price_optuna_v2 import train_price_optuna_v2


def main():
    output=ROOT/'outputs/optuna_v3_metric_fix_20261016';_,_,_,data=fixture(4,2)
    for row,x,y in zip(data['agreements'],(0.,1000000.,.000001,2000000.),(100.,200.,300.,400.)):
        row['params']['x0']=x;row['params']['x1']=0.;row['historical_price_wan']=y
    results=[]
    for engine in ('optuna_v2','optuna_v3'):
        target=output/('extrapolation_'+engine)
        cfg={'search':{'n_trials':10}}
        if engine=='optuna_v3':cfg['v3']={'startup_trials':1,'warm_start':{'auto_history':False}}
        try:
            _,report=train_price_optuna_v2(data,target,overrides=cfg,engine=engine,progress=print)
            item={'engine':engine,'status':'PASS','report':report['validation_metrics']}
        except Exception as exc:item={'engine':engine,'status':'ERROR','error':repr(exc)}
        results.append(item);print(json.dumps(item,ensure_ascii=False),flush=True)
    (output/'extrapolation_reproduction.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
