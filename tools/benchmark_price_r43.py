# -*- coding: utf-8 -*-
"""Fixed 16x36 smoke cases; fast bounded search, no private data required."""
import argparse
import copy
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import numpy as np
from product_price_r43_test import fixture,small_config
from tools.product_price_automl_r4 import train_price_r4,score
from services.price_service.native_bundle import predict_batch


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);a=p.parse_args();out=Path(a.output);out.mkdir(parents=True,exist_ok=True)
    results=[]
    for name,noise in [('quadratic_noisy',False),('random_price_control',True)]:
        X,y,specs,data=fixture(noise);directory=out/name;directory.mkdir(parents=True,exist_ok=True)
        (directory/'fixed_training_records.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        b,r=train_price_r4(copy.deepcopy(data),directory/'training',overrides=small_config())
        rng=np.random.RandomState(4809);external=rng.uniform(-1,1,(256,36))
        truth=rng.uniform(50,150,256) if noise else 80+30*external[:,0]**2+rng.normal(0,1.,256)
        predictions=predict_batch(b,[{s['parameter_id']:float(row[j]) for j,s in enumerate(specs)} for row in external])
        result={'case':name,'audit':r['nested_cv_metrics'],'development':r['cv_metrics'],'external_256':score(truth,[v['predicted_price_wan'] for v in predictions]),
                'seconds':r['elapsed_seconds'],'configurations':r['configuration_count'],'targets_met':r['targets_met'],'ensemble_requirement_met':r['ensemble_requirement_met'],
                'note':'fixed virtual software tests only; no business accuracy certification'}
        results.append(result);print(json.dumps(result,ensure_ascii=False),flush=True)
    (out/'validation.json').write_text(json.dumps(results,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
