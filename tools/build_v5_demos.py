# -*- coding: utf-8 -*-
import json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
app.__path__.insert(0,str(ROOT/'app'))
from automl_pipeline.product_pipeline import build
from automl_pipeline.product_price_optuna_v5 import load_config
from tools.product_price_automl import write_json


def main():
    evidence=ROOT/'outputs/v5_20261023';reports=[]
    for mode in ('goal_first','balanced','robust'):
        directory=evidence/('demo_v5_'+mode)
        if (directory/'prototype_manifest.json').is_file():continue
        cfg=load_config(overrides={'v5':{'mode':mode,'persistence':{'directory':str(evidence/'demo_checkpoints')}}})
        path=evidence/('demo_'+mode+'_config.json');write_json(path,cfg)
        build(ROOT/'automl_pipeline/examples/virtual_16x36.xlsx',directory,product_code='PORTABLE_PRODUCT',product_name='成品价格效能与推荐（V5虚拟演示）',
              virtual_data=True,price_engine='optuna_v5',price_config=path)
        reports.append(json.loads((directory/'price/training_report.json').read_text(encoding='utf-8')))
    write_json(evidence/'demo_reports.json',reports)
if __name__=='__main__':main()
