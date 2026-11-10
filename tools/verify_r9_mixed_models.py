"""Verify real V3 and DS assets can replace one another in the same frozen application."""
import argparse,base64,copy,io,json,os,subprocess,sys,time,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json
from product.launcher import fetch,running
from tools.client_builder import free_ports
from automl_pipeline.product_price_optuna_v3 import train_price_optuna_v3
from services.price_service.native_bundle import load_bundle,predict
from threadpoolctl import threadpool_limits
p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--label',default='2');a=p.parse_args();package=Path(a.package).resolve()
out=ROOT/'outputs'/('r9_mixed_models'+a.label);out.mkdir(exist_ok=False);manifest=read_json(package/'PACKAGE_MANIFEST.json')
business=read_json(package/manifest['release']['business_package'])['data']
for parameter in business['parameters']:
 if parameter['value_type']=='boolean':parameter['value_type']='number'
for i,row in enumerate(business['agreements']):
 row['historical_price_wan']=5.+.1*i
 row['params']={k:int(v) if isinstance(v,bool) else v for k,v in row['params'].items()}
with threadpool_limits(limits=1):
 _,training=train_price_optuna_v3(business,out/'trained',overrides={'search':{'n_trials':6},'v3':{'startup_trials':1}},progress=lambda s:None)
native=out/'trained/price_native_bundle.pkl';model=load_bundle(native)
stream=io.BytesIO()
with zipfile.ZipFile(stream,'w',zipfile.ZIP_DEFLATED) as z:
 z.write(str(native),native.name);z.write(str(out/'trained/training_report.json'),'training_report.json')
data=out/'userdata';settings=free_ports();write_json(data/'config/desktop.json',settings);base='http://127.0.0.1:%d'%settings['ports']['web'];process=None;report={'checks':{}}
def check(k,v):
 report['checks'][k]=bool(v);write_json(out/'report.json',report)
 if not v:raise AssertionError(k)
def call(path,body=None):return fetch(base+path,body,timeout=110)
def start():
 global process
 process=subprocess.Popen([str(package/'product.exe'),'--data-dir',str(data),'--no-browser'],creationflags=subprocess.CREATE_NO_WINDOW)
 for _ in range(240):
  try:
   if call('/api/bootstrap')['integration']['calculation_available']:return
  except Exception:pass
  time.sleep(.25)
 raise ValueError('EXE readiness timeout')
def stop():
 current=running(data)
 if current:
  try:fetch(current[0]['url']+'/api/stop',{},current[0]['token'])
  except Exception:pass
 if process:process.wait(timeout=30)
def install(name,filename,content):return call('/api/model-library/install',{'role':'price','name':name,'filename':filename,'content':base64.b64encode(content).decode('ascii')})
def activate(asset_id):call('/api/model-library/activate',{'role':'price','id':asset_id})
def pure():
 s=call('/api/deep-price/v1/schema');params=call('/api/deep-price/v1/products?page=1&page_size=1')['items'][0]['parameters']
 body={k:s[k] for k in ('product_code','release_id','model_version','price_model_sha256','model_instance_id','business_snapshot_sha256','price_output_signature')};body['parameters']=params
 return call('/api/deep-price/v1/predict',body),params
try:
 start();initial,params=pure();check('baseline_ds',abs(initial['predicted_price_wan']-4.5)<1e-8)
 ml=install('V3 mixed test','native.zip',stream.getvalue());activate(ml['id']);actual,params=pure();expected=predict(model,params)['predicted_price_wan']
 check('v3_same_business_native_consistency',abs(actual['predicted_price_wan']-expected)<1e-8)
 # Raw SDK model import exercises automatic label-to-English mapping.
 ds=read_json(ROOT/'examples/ds/DEMO_model.json');ds['base']+=1000
 imported=install('DS mixed test','DS_model.json',json.dumps(ds,ensure_ascii=False).encode('utf-8'));activate(imported['id'])
 changed,_=pure();check('raw_ds_import_and_replacement',abs(changed['predicted_price_wan']-4.6)<1e-8)
 activate(ml['id']);stop();start();persisted,_=pure()
 check('v3_choice_survives_cold_restart',abs(persisted['predicted_price_wan']-expected)<1e-8)
 check('both_backend_assets_retained',len(call('/api/model-library')['items'])==2)
 report.update(status='pass',predictions={'ds':initial['predicted_price_wan'],'v3':expected,'replaced_ds':changed['predicted_price_wan']})
except Exception as e:report.update(status='fail',error=repr(e));raise
finally:stop();write_json(out/'report.json',report)
print(json.dumps(report,ensure_ascii=True,indent=2))
