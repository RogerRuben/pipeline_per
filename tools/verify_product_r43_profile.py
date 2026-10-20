# -*- coding: utf-8 -*-
import json
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_price_r43_test import fixture
from tools.product_price_automl_r4 import InnerSearch
from tools.product_r43_profiles import profile
from threadpoolctl import threadpool_limits
import numpy as np


def main():
    out=ROOT/'outputs/r43_validation_20261015/production_quick_profile';out.mkdir(parents=True,exist_ok=True)
    X,y,specs,data=fixture();c=profile('quick');start=time.monotonic()
    with threadpool_limits(limits=1):r=InnerSearch(X,y,np.arange(16),specs,[3]*36,c,start+30,out,lambda t:None).run()
    result={'seconds':time.monotonic()-start,'diagnostics':r['diagnostics'],'seed_report':r['seed_report'],'validation':r['validation_summary']}
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'seconds':result['seconds'],'coverage':r['diagnostics']['coverage'],'seed_report':r['seed_report']},ensure_ascii=False))
    assert not r['diagnostics']['coverage']['unattempted_models']
if __name__=='__main__':main()
