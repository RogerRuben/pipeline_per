"""Deterministic synthetic products for real Optuna V3 training acceptance."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import random
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.xlsx_utils import write_workbook_bytes


def prepare(output,rows=160):
    output=Path(output).resolve()
    if output.exists() or ROOT not in output.parents or output.parts[len(ROOT.parts)]!='outputs':
        raise ValueError('输出须为工程outputs下的新目录')
    output.mkdir(parents=True)
    rng=random.Random(20261031)
    fields=['numeric_%02d'%i for i in range(24)]+['integer_%02d'%i for i in range(12)]+['flag_%02d'%i for i in range(10)]+['category_%02d'%i for i in range(8)]+['optional_%02d'%i for i in range(6)]
    labels=['连续参数%02d(单位)'%i for i in range(24)]+['部件数量%02d(个)'%i for i in range(12)]+['是否配置功能%02d'%i for i in range(10)]+['部件配置%02d'%i for i in range(8)]+['可选性能%02d(单位)'%i for i in range(6)]
    body=[]
    for index in range(rows):
        numeric=[round(rng.uniform(10,100),5) for _ in range(24)]
        integer=[rng.randrange(1,11) for _ in range(12)]
        flags=[rng.choice(['有','无']) for _ in range(10)]
        categories=[rng.choice(['基础','增强','高配']) for _ in range(8)]
        optional=[round(rng.uniform(1,20),5) for _ in range(6)]
        price=math.exp(2+.005*numeric[0]+.003*numeric[1]+.02*integer[0]+.08*(flags[0]=='有')+.06*['基础','增强','高配'].index(categories[0])+rng.gauss(0,.015))
        body.append(['SYN-%03d'%(index+1),'虚拟产品%03d'%(index+1),round(price,6)]+numeric+integer+flags+categories+optional)
    workbook=write_workbook_bytes([('成品数据',[['成品编号','成品名称','价格(万元)']+labels,['product_id','product_name','price_wan']+fields]+body)])
    (output/'products.xlsx').write_bytes(workbook)
    config=json.loads((ROOT/'automl_pipeline/configs/price_optuna_v3.json').read_text(encoding='utf-8'))
    config['search'].update(n_trials=40,timeout_seconds=600)
    config['expert']['field_types']={key:('enum' if key.startswith(('flag_','category_')) else 'number') for key in fields}
    config['v3']['warm_start']['auto_history']=False
    (output/'v3_config.json').write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'provenance.json').write_text(json.dumps({'classification':'synthetic','seed':20261031,'rows':rows,'attributes':len(fields),'source_sha256':hashlib.sha256(workbook).hexdigest(),'engine':'optuna_v3','profile':'existing quick40; 600s cap; no algorithm changes','training_missing_policy':'complete numeric training inputs required by unchanged V3; missing values tested as API negative cases','accuracy_scope':'software acceptance only, not real business certification'},indent=2)+'\n',encoding='utf-8')
    print(str(output/'products.xlsx'))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--rows',type=int,default=160);a=p.parse_args();prepare(a.output,a.rows)
