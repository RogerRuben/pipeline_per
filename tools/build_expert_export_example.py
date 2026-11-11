"""Synthetic evidence using the supplied expert application's real export method."""
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from services.effectiveness_service.expert_packages import module,read_export,ExpertBackend
from product.common import read_json,write_json,sha
from openpyxl import Workbook,load_workbook
def build(out,family):
 out=Path(out);out.mkdir(parents=True,exist_ok=False)
 data=read_json(ROOT/'outputs/r9_verified_v3/business_release.iprelease.json')['data'];defs={d['parameter_id']:d for d in data['parameters']};keys=['numeric_00','numeric_01'];labels=[defs[k]['label'] for k in keys]
 wb=Workbook();ws=wb.active;ws.title='方案数据';ws.append(['方案编号','来源','已知可行性']+labels)
 for i in range(12):ws.append(['T%02d'%i,'virtual','可行',30+i*3,70-i*2])
 ws=wb.create_sheet('项目信息');ws.append(['成品代号','EXPERT_EXPORT_DEMO']);ws.append(['成品名称','原程序导出接入演示'])
 ws=wb.create_sheet('属性配置');ws.append(['属性名称','属性ID','单位','数据类型','生成下限','生成上限','可行下限','可行上限','偏好方向','边际规律','设计阶段'])
 for k,label in zip(keys,labels):ws.append([label,k,defs[k].get('unit') or '', 'continuous',10,100,0,150,'越大越好','线性',2])
 ws=wb.create_sheet('评价基准');ws.append(['基准编号','基准名称','属性名','要求类型','目标值']);
 for label in labels:ws.append(['BASE','标准基准',label,'target',50])
 base=out/'product.xlsx';wb.save(str(base))
 if family=='pump':
  excel=module('pump','system_excel');product=module('pump','project_excel').load_project_workbook(base)
  attrs=[excel.SystemAttributeSpec('system_'+k,'系统'+label,defs[k].get('unit') or '') for k,label in zip(keys,labels)]
  maps=[excel.SystemMappingSpec('M'+str(i),k,label,defs[k].get('unit') or '', 'direct_system','system_'+k,'系统'+label,defs[k].get('unit') or '', 'linear') for i,(k,label) in enumerate(zip(keys,labels))]
  tasks=[excel.SystemTask('TASK_'+str(j),'工况'+str(j),requirements=tuple(excel.SystemRequirementSpec(a.key,a.label,a.unit,'target',target_value=45+10*j,lower_80_distance=12,upper_80_distance=12) for a in attrs)) for j in range(2)]
  excel._write_v2_contract(wb,attrs,maps,tasks);path=out/'pump.xlsx';wb.save(str(path))
  app=module('pump','system_app').SystemProjectApp(path,out/'state')
  state=app.load_state(reset=True)
  app.save_state(state)
  # The public app export method is exactly the one invoked by its download UI.
  content,name=app.export_frozen_model_bundle()
 else:
  app=module('lock','lock_v17_app').LockV17App(base,out/'state');state=app.load_state(reset=True)
  for _ in range(6):
   pair=app.active_session(app.load_state())['active_pair'];app.submit({'reuseB':'reusable' if pair['probe_d']<.3 else 'not_reusable'})
  content,name=app.export_frozen_model_bundle()
 package=out/name;package.write_bytes(content);raw=read_export(content);model=out/'model.json';model.write_bytes(raw)
 backend=ExpertBackend(model);reference=[]
 for params in [{'numeric_00':50.,'numeric_01':50.},{'numeric_00':42.,'numeric_01':63.},{'numeric_00':70.,'numeric_01':30.}]:
  for profile in backend.schema()['protocol_profiles']:
   selector=profile['id'];original=backend.runtime.evaluate(params,task_id=selector) if family=='pump' else backend.runtime.evaluate(params,profile_id=selector)
   adapted=backend.evaluate(params,selector);assert abs(original['effectiveness_score']-adapted['effectiveness_score'])<1e-9
   if family=='pump':
    online_state=app.load_state();online_state['active_system_task_id']=selector
    online=app.evaluate(params,online_state)
    assert abs(online['effectiveness_score']-original['effectiveness_score'])<1e-6,(online['effectiveness_score'],original['effectiveness_score'])
   reference.append({'parameters':params,'target_protocol':selector,'score':original['effectiveness_score'],'raw':original})
 write_json(out/'expected.json',{'family':family,'package':str(package),'model_sha256':sha(model),'schema':backend.schema(),'cases':reference})
 print(json.dumps({'status':'pass','package':str(package),'comparisons':len(reference)},ensure_ascii=True))
if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--family',choices=['pump','lock'],default='pump');a=p.parse_args();build(a.out,a.family)
