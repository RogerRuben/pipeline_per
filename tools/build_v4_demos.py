# -*- coding: utf-8 -*-
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import app
app.__path__.insert(0,str(ROOT/'app'))
from tools.product_pipeline import build
from tools.product_price_optuna_v4 import load_config
from tools.product_price_automl import write_json


def main():
    evidence=ROOT/'outputs/optuna_v4_20261016';records=[]
    for mode in ('hybrid','random','global','per_split'):
        output=evidence/('demo_v4_'+mode)
        if (output/'prototype_manifest.json').is_file():continue
        cfg=load_config(overrides={'search':{'n_trials':48},'v4':{'search':{'mode':mode},'splits':{'max_splits':12},'review':{'max_candidates':4},'ensemble':{'weight_trials':8}}})
        cfg_path=evidence/('demo_'+mode+'_config.json');write_json(cfg_path,cfg)
        build(ROOT/'outputs/optuna_dual_20261016/products.xlsx',output,product_code='PORTABLE_PRODUCT',product_name='成品价格效能与推荐（V4虚拟演示）',virtual_data=True,price_engine='optuna_v4',price_config=cfg_path)
        report=json.loads((output/'price/training_report.json').read_text(encoding='utf-8'));records.append({'mode':mode,'report':report})
    write_json(evidence/'demo_reports.json',records)
if __name__=='__main__':main()
