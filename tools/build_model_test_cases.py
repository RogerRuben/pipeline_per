"""Create explicitly synthetic, mutually compatible model-library acceptance assets."""
import argparse,copy,json,pickle,sys,zipfile,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha
from services.price_service.native_bundle import load_bundle,save_bundle,predict
from services.price_service.ds_backend import DSPriceService
from services.price_service.ds_engine import _DSModel
from automl_pipeline.product_price_optuna_v3 import train_price_optuna_v3
from threadpoolctl import threadpool_limits

def build(out,release,trained_b=None):
 out,release=Path(out),Path(release);out.mkdir(parents=True,exist_ok=False)
 document=read_json(release/'business_release.iprelease.json');data=document['data'];defs={p['parameter_id']:p for p in data['parameters']}
 def zip_model(path,files):
  with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as z:
   for name,source in files.items():z.write(str(source),name)
 zip_model(out/'A_V3_baseline.zip',{'price_native_bundle.pkl':release/'price/price_native_bundle.pkl','training_report.json':release/'price/training_report.json'})
 changed=copy.deepcopy(data)
 for row in changed['agreements']:
  v=row['params'];row['historical_price_wan']=float(row['historical_price_wan']*1.18+.0015*v['numeric_00']**2)
 if trained_b:shutil.copytree(str(trained_b),str(out/'build_B'))
 else:
  with threadpool_limits(limits=1):
   _,report=train_price_optuna_v3(changed,out/'build_B',overrides={'search':{'n_trials':8},'v3':{'startup_trials':1,'warm_start':{'auto_history':False}}},progress=lambda s:None)
 bundle=load_bundle(out/'build_B/price_native_bundle.pkl');bundle['model_version']='DEMO-V3-B-20261111';save_bundle(out/'build_B/price_native_bundle.pkl',bundle)
 zip_model(out/'B_V3_nonlinear.zip',{'price_native_bundle.pkl':out/'build_B/price_native_bundle.pkl','training_report.json':out/'build_B/training_report.json'})
 model=read_json(ROOT/'examples/ds/DEMO_model.json');model['base']=10000.
 feature=copy.deepcopy(model['catalog']['features']['接口数量']);feature['name']='尺寸加工量'
 for rule in feature['rules']:rule['name']='尺寸加工量'
 model['catalog']['features']={'尺寸加工量':feature}
 categories=json.loads(defs['category_00']['allowed_values_json']);template=copy.deepcopy(next(iter(model['catalog']['sensors'].values())))
 model['catalog']['sensors']={name:dict(template,name=name,value=20000.+i*5000,low=20000.+i*5000,high=20000.+i*5000) for i,name in enumerate(categories)}
 required=json.loads(_DSModel(model).schema_json())['required'];assert len(required)==3,required
 mapping={'category_00':required[0]['name'],'integer_00':required[1]['name'],'numeric_00':'尺寸加工量'}
 fields=[]
 for key,name in mapping.items():
  d=defs[key];field={'field_name':key,'field_label':d['label'],'ds_name':name,'dtype':'enum' if key=='category_00' else 'integer' if key=='integer_00' else 'number','unit':d.get('unit') or '', 'required':True,'missing_policy':'reject'}
  if key=='category_00':
   encoded=json.loads(d.get('model_value_mapping_json') or '{}');field['value_aliases']={str(v):k for k,v in encoded.items()};field['value_aliases'].update({str(float(v)):k for k,v in encoded.items()});field['allowed_values']=list(encoded.values())
  fields.append(field)
 wrapped={'format':'pipeline-ds-release-1.0','product_code':document['product_code'],'product_name':document['product_name'],'virtual_data':True,'model':model,'fields':fields,'reference_parameters':data['agreements'][0]['params']}
 write_json(out/'C_three_layer_ready.json',wrapped);write_json(out/'C_raw_model_optional.json',model);write_json(out/'C_raw_field_mapping_optional.json',mapping)
 effect=read_json(release/'effectiveness/effectiveness_bundle.json');effect['model_version']='DEMO-EFFECT-20261111';write_json(out/'D_effectiveness_demo.json',effect)
 ds=DSPriceService(out/'C_three_layer_ready.json');rows=[]
 for row in data['agreements'][:3]:
  params=row['params'];runtime=dict(params)
  for key,d in defs.items():runtime[key]=json.loads(d.get('model_value_mapping_json') or '{}').get(str(params.get(key)),params.get(key))
  modified=dict(params,numeric_00=min(defs['numeric_00']['max_value'],params['numeric_00']+5));encoded=dict(runtime,numeric_00=modified['numeric_00'])
  rows.append({'product_id':row['agreement_id'],'parameters':params,'historical_price_wan':row['historical_price_wan'],'A_model_price_wan':predict(load_bundle(release/'price/price_native_bundle.pkl'),runtime)['predicted_price_wan'],'B_model_price_wan':predict(bundle,runtime)['predicted_price_wan'],'C_model_price_wan':ds.handle_post('/api/v1/predict',{'parameters':runtime})['prediction']['predicted_price_wan'],'modified_parameters':modified,'modified_prices_wan':{'A':predict(load_bundle(release/'price/price_native_bundle.pkl'),encoded)['predicted_price_wan'],'B':predict(bundle,encoded)['predicted_price_wan'],'C':ds.handle_post('/api/v1/predict',{'parameters':encoded})['prediction']['predicted_price_wan']}})
 write_json(out/'expected_predictions.json',{'classification':'synthetic','price_unit':'wan_yuan','rows':rows})
 (out/'使用说明.md').write_text('''# 模型库测试案例（虚拟数据）

本组案例搭配 R11 AutoML 演示运行包的 160 行、60 属性数据。不是甲方业务模型；英文字段不同的实际产品不能强行套用。

先解压测试案例总包；不要把整个总包上传模型库。在 Portal 顶部“模型库”选择价格模型，依次上传内部的 A_V3_baseline.zip、B_V3_nonlinear.zip、C_three_layer_ready.json，分别检查保存，再切换当前模型。A/B 保留 ZIP 文件即可，不必再解压。

每次切换后，价格页刷新，选择下表成品，将 numeric_00 改为表中值，再点击预测；其他属性恢复成品默认值即可。预期价格均为万元，允许页面显示的小数舍入。

A 是运行包现有 V3 模型；B 用同一套字段和明确修改后的虚拟价格重新训练，预测应有区别；C 是已封装的三层演示模型，使用 category_00（部件型号）、integer_00（部件数量）与 numeric_00（尺寸加工量）演示分层计价，其他属性不参与该模型。C 不提供 AutoML 训练指标，显示未提供是正常的。

注意：选中历史成品且不改属性时，页面按需求显示原成交价，不会因为换模型改变。验证模型差异时，修改 numeric_00 等属性再预测；或通过纯模型预测 API 核对 expected_predictions.json 的完整参数。

D_effectiveness_demo.json 选择“效能模型”导入，验证独立切换、名称和重启持久化。其算法与试用快照一致，只用于测试接入，不是正式泵专家模型。

“三层模型字段映射”不是训练：左边是系统稳定英文字段，右边是对方原始 DS 模型要求的名称。C_three_layer_ready.json 已内置对应关系，直接导入，无需填任何映射。

只有想额外测试原始 SDK 导入时，才选择 C_raw_model_optional.json，并将 C_raw_field_mapping_optional.json 的内容填入高级字段对照。直接导入已封装 C 是通常流程。

恢复时切回 A 或“使用预装模型”；退出软件并重开，模型库与当前选择应保留。若在已修改过的同字段数据上测试，模型预测以当前实际属性为准。
''',encoding='utf-8')
 with (out/'使用说明.md').open('a',encoding='utf-8') as guide:
  guide.write('\n| 成品编号 | numeric_00 改为 | A 预测 | B 预测 | C 预测 |\n| --- | ---: | ---: | ---: | ---: |\n')
  for row in rows:guide.write('| '+row['product_id']+' | '+str(row['modified_parameters']['numeric_00'])+' | '+' | '.join('%.4f'%row['modified_prices_wan'][k] for k in ('A','B','C'))+' |\n')
 files=[p for p in out.iterdir() if p.is_file()];write_json(out/'CASE_MANIFEST.json',{'classification':'synthetic','compatible_product_code':document['product_code'],'files':{p.name:sha(p) for p in files}})
 archive=out.with_suffix('.zip')
 with zipfile.ZipFile(archive,'x',zipfile.ZIP_DEFLATED) as z:
  for p in out.iterdir():
   if p.is_file():z.write(str(p),out.name+'/'+p.name)
 return {'archive':str(archive),'bytes':archive.stat().st_size,'sha256':sha(archive)}

if __name__=='__main__':
 p=argparse.ArgumentParser();p.add_argument('--out',required=True);p.add_argument('--release',required=True);p.add_argument('--trained-b');a=p.parse_args();print(json.dumps(build(a.out,a.release,a.trained_b),ensure_ascii=True,indent=2))
