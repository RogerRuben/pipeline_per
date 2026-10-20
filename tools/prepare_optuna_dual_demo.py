# -*- coding: utf-8 -*-
"""Prepare synthetic numeric/configuration records; author the workbook with Artifact Tool."""
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v3_test import mixed_fixture
from tools.product_price_optuna_v2 import load_config as v2_config
from tools.product_price_optuna_v3 import load_config as v3_config


def main():
    output=ROOT/'outputs/optuna_dual_20261016';output.mkdir(parents=True,exist_ok=True);data=mixed_fixture()
    data['products']=[{'product_code':'PORTABLE_PRODUCT','product_name':'成品价格效能与推荐（V2/V3虚拟演示）'}]
    names=['连接点数量','额定出力(kN)','维护周期(小时)','驱动方式','安装配置代码']
    for i,p in enumerate(data['parameters']):p['label']=names[i] if i<5 else '性能属性%02d'%(i+1)
    for row in data['agreements']:row['product_code']='PORTABLE_PRODUCT'
    specs=data['parameters'];rows=[['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs],['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs]]
    rows.extend([r['agreement_id'],'虚拟成品%02d'%(i+1),r['historical_price_wan']]+[r['params'][p['parameter_id']] for p in specs] for i,r in enumerate(data['agreements']))
    scores={'x0':5,'x1':1,'x4':4};ratings=[['英文代号','中文名','价格重要性评分','属性类型']]
    ratings.extend([p['parameter_id'],p['label'],scores.get(p['parameter_id']),'配置类型' if p['parameter_id'] in ('x3','x4') else '数值'] for p in specs)
    (output/'workbook_data.json').write_text(json.dumps({'sheets':[{'name':'成品数据','rows':rows},{'name':'价格属性评分','rows':ratings}]},ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'fixture.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'demo_v2_config.json').write_text(json.dumps(v2_config(overrides={'search':{'n_trials':6}}),ensure_ascii=False,indent=2),encoding='utf-8')
    (output/'demo_v3_config.json').write_text(json.dumps(v3_config(overrides={'search':{'n_trials':10},'v3':{'startup_trials':2,'warm_start':{'auto_history':False}}}),ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
