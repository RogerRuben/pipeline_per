# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
app.__path__.insert(0,str(ROOT/'app'))
from tools.product_pipeline import build


def main():
    base=ROOT/'outputs/optuna_dual_20261016'
    for version in ('v2','v3'):
        result=build(base/'products.xlsx',base/('demo_'+version),product_code='PORTABLE_PRODUCT',
            product_name='成品价格效能与推荐（V2/V3虚拟演示）',virtual_data=True,price_engine='optuna_'+version,
            price_config=base/('demo_'+version+'_config.json'),missing_tokens=[])
        print(json.dumps(result,ensure_ascii=False),flush=True)
if __name__=='__main__':main()
