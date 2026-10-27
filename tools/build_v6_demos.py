# -*- coding: utf-8 -*-
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
from product_optuna_v6_test import data_fixture
from product_optuna_v2_test import workbook
from automl_pipeline.product_pipeline import build,write_json
from automl_pipeline.product_price_optuna_v6 import load_config

def main():
    p=argparse.ArgumentParser();p.add_argument('--evidence',default=str(ROOT/'outputs/v6_20261027/final_demos'));a=p.parse_args()
    evidence=Path(a.evidence).resolve();evidence.mkdir(parents=True,exist_ok=True)
    source=ROOT/'automl_pipeline/examples/virtual_v6_16x36.xlsx'
    if not source.is_file():source.write_bytes(workbook(data_fixture(16,36)))
    reports=[]
    for mode in ('goal_first','balanced','robust'):
        directory=evidence/('demo_v6_'+mode)
        if not (directory/'prototype_manifest.json').is_file():
            cfg=load_config(overrides={'expert':{'scores':{'x0':5,'x1':4},'field_types':{'x%d'%i:('enum' if i in (3,4) else 'number') for i in range(36)}},
                'v6':{'mode':mode,'persistence':{'directory':str(evidence/'demo_checkpoints')}}})
            path=evidence/('demo_'+mode+'_config.json');write_json(path,cfg)
            build(source,directory,product_code='PORTABLE_PRODUCT',product_name='成品价格效能与推荐（V6虚拟演示）',
                  virtual_data=True,price_engine='optuna_v6',price_config=path)
        reports.append(json.loads((directory/'price/training_report.json').read_text(encoding='utf-8')))
    write_json(evidence/'demo_reports.json',reports)

if __name__=='__main__':main()
