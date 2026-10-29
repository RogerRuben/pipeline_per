# -*- coding: utf-8 -*-
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
import app
app.__path__.insert(0,str(ROOT/'app'))
from tools.benchmark_product_v7 import world
from app.xlsx_utils import write_workbook_bytes
from automl_pipeline.product_price_optuna_v7 import load_config
from automl_pipeline.product_pipeline import build,write_json

def main():
    p=argparse.ArgumentParser();p.add_argument('--output',default=str(ROOT/'outputs/v7_20261030/release_demos'));p.add_argument('--refresh-input',action='store_true');a=p.parse_args()
    evidence=Path(a.output).resolve();evidence.mkdir(parents=True,exist_ok=True)
    source=ROOT/'automl_pipeline/examples/virtual_v7_16x36.xlsx'
    if a.refresh_input or not source.is_file():
        data=world(16,36,1000,.04,.7);specs=data['parameters']
        rows=[['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs],['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs]]
        for i,r in enumerate(data['agreements']):rows.append([r['agreement_id'],'虚拟成品%d'%(i+1),r['historical_price_wan']]+[r['params'][p['parameter_id']] for p in specs])
        grades=[['英文代号','中文名','价格重要性评分','属性类型']]+[[p['parameter_id'],p['label'],5 if p['parameter_id']=='x0' else 4 if p['parameter_id']=='x5' else 3 if p['parameter_id'] in ('x1','x2') else 2 if p['parameter_id']=='x3' else 1,'enum' if p['parameter_id'] in ('x3','x4') else 'number'] for p in specs]
        source.write_bytes(write_workbook_bytes([('成品数据',rows),('价格属性评分',grades),('属性类型',[['英文代号','属性类型'],['x3','enum'],['x4','enum']])]))
    reports=[]
    for mode in ('balanced','goal_first','baseline_only'):
        out=evidence/('demo_v7_'+mode)
        if not (out/'prototype_manifest.json').is_file():
            cfg=load_config(overrides={'search':{'n_trials':40,'timeout_seconds':300},'expert':{'field_types':{'x3':'enum','x4':'enum'}},
                'v7':{'mode':mode,'persistence':{'directory':str(evidence/'checkpoints')}}})
            build(source,out,product_code='PORTABLE_PRODUCT',product_name='成品价格效能与推荐（V7/V3+虚拟演示）',virtual_data=True,
                price_engine='optuna_v7',price_overrides=cfg)
        reports.append(json.loads((out/'price/training_report.json').read_text(encoding='utf-8')))
    write_json(evidence/'demo_reports.json',reports)
if __name__=='__main__':main()
