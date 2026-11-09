"""Synthetic DS workbook, used only for onboarding and software acceptance."""
from app.xlsx_utils import write_workbook_bytes
from services.price_service.ds_engine import load_model
import json


def workbook(model_path):
    model=load_model(model_path)
    keys=['component_type','component_count','alarm','interface_count','accuracy','communication','enhanced']
    names=['部件型号','部件数量','报警功能','接口数量','精度等级','通信类型','增强采集功能']
    rows=[['成品编号','成品名称','价格(万元)']+names,['product_id','product_name','price_wan']+keys]
    for i in range(8):
        values=['型号1',1+i%2,bool(i%2),i%2,'一级','标准',False]
        result=json.loads(model.predict_json(json.dumps(dict(zip(names,values)),ensure_ascii=False)))
        if not result['success']:raise ValueError(result)
        rows.append(['DS-%02d'%i,'演练产品%02d'%i,result['total_price']/10000.]+values)
    return write_workbook_bytes([('成品数据',rows),('产品信息',[['字段','值'],['产品名称','三层价格演练']])])
