"""Actual frozen EXE regressions; each run owns its isolated userdata and processes."""
import argparse,base64,io,json,os,sqlite3,subprocess,sys,time,zipfile
from contextlib import closing
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha
from product.launcher import fetch,running
from tools.client_builder import free_ports
p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--label',required=True);p.add_argument('--browser',action='store_true');a=p.parse_args()
package=Path(a.package).resolve();out=ROOT/'outputs'/('r9_native_'+a.label);out.mkdir(exist_ok=False)
data=out/'userdata';settings=free_ports();write_json(data/'config/desktop.json',settings)
base='http://127.0.0.1:%d'%settings['ports']['web'];report={'checks':{},'win7_machine_tested':False};process=None
def check(k,v):
 report['checks'][k]=bool(v);write_json(out/'report.json',report)
 if not v:raise AssertionError(k)
def call(path,body=None):return fetch(base+path,body,timeout=110)
def stop():
 global process
 current=running(data)
 if current:
  try:fetch(current[0]['url']+'/api/stop',{},current[0]['token'])
  except Exception:pass
 if process:
  try:process.wait(timeout=25)
  except subprocess.TimeoutExpired:process.kill();process.wait()
 process=None
def start():
 global process
 process=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data)],creationflags=subprocess.CREATE_NO_WINDOW)
 deadline=time.monotonic()+90
 while time.monotonic()<deadline:
  if process.poll() is not None:raise ValueError('EXE exited: inspect isolated logs')
  try:
   if call('/api/bootstrap')['integration']['calculation_available']:return
  except Exception:pass
  time.sleep(.3)
 raise ValueError('EXE readiness timeout')
def status():return fetch(running(data)[0]['url']+'/api/status')
def control(role,action):
 current=running(data)[0];op=fetch(current['url']+'/api/services/'+role+'/'+action,{},current['token'])
 end=time.monotonic()+75
 while time.monotonic()<end:
  operation=next((v for v in status()['operations'] if v['id']==op['operation_id']),{})
  if operation.get('status')=='succeeded':return
  if operation.get('status')=='failed':raise ValueError(operation.get('error'))
  time.sleep(.25)
 raise ValueError('service control timeout')
def upload(role,name,path):
 return call('/api/model-library/install',{'role':role,'name':name,'filename':Path(path).name,'content':base64.b64encode(Path(path).read_bytes()).decode('ascii')})
def check_role_restart(role,asset_id):
 before=status()['services'];call('/api/model-library/activate',{'role':role,'id':asset_id});after=status()['services']
 check(role+'_switch_restarts_only_itself_'+asset_id[:6],before[role]['pid']!=after[role]['pid'] and all(before[r]['pid']==after[r]['pid'] for r in before if r!=role))
try:
 start();check('fresh_exe_start',True)
 if a.browser:
  with (out/'browser.log').open('wb') as log:
   done=subprocess.run(['C:/Program Files/nodejs/node.exe',str(ROOT/'tools/verify_r9_ui.mjs'),base,str(out/'browser')],stdout=log,stderr=subprocess.STDOUT,timeout=300)
  check('complete_browser_checks',done.returncode==0)
 else:
  schema=call('/api/deep-price/v1/schema');ref=call('/api/deep-price/v1/products?page=1&page_size=100')['items'][0]
  payload={k:schema[k] for k in ('product_code','release_id','model_version','price_model_sha256','model_instance_id','business_snapshot_sha256','price_output_signature')};payload.update(parameters=ref['parameters'],reference_product_id=ref['product_id'])
  check('ds_historical_price',call('/api/deep-price/v1/reference-price',payload)['predicted_price_wan']==ref['historical_price_wan'])
  model_payload=dict(payload);model_payload.pop('reference_product_id')
  pure=call('/api/deep-price/v1/predict',model_payload);check('ds_model_layers',abs(pure['predicted_price_wan']-4.5)<1e-8)
 # Deliberate legacy/release drift must never prevent the web UI from starting.
 database=data/'data/protocol_demo.db'
 with closing(sqlite3.connect(str(database))) as c,c:
  c.execute('CREATE TABLE qa_sentinel(value TEXT)');c.execute('INSERT INTO qa_sentinel VALUES (?)',('用户保留记录',))
  c.execute("UPDATE parameter_definitions SET min_value=min_value-0.01 WHERE parameter_id=(SELECT parameter_id FROM parameter_definitions WHERE min_value IS NOT NULL LIMIT 1)")
 stop();start();check('cold_restart_with_live_schema_drift',call('/api/health')['status']=='ok')
 with closing(sqlite3.connect(str(database))) as c:
  check('userdata_not_reseeded',c.execute('SELECT value FROM qa_sentinel').fetchone()[0]=='用户保留记录')
 if a.browser:
  check('chinese_renames_survive_cold_restart','验收新中文_' in json.dumps(call('/api/deep-price/v1/schema'),ensure_ascii=False))
 # Import a real native model (including evaluation metadata) from the package.
 manifest=read_json(package/'PACKAGE_MANIFEST.json');release=manifest['release'];model=package/release['price_model']
 memory=io.BytesIO()
 with zipfile.ZipFile(memory,'w',zipfile.ZIP_DEFLATED) as z:
  z.write(str(model),model.name)
  for name in ('evaluation_metadata.json','training_report.json'):
   if (model.parent/name).is_file():z.write(str(model.parent/name),name)
 asset=out/'price_asset.zip';asset.write_bytes(memory.getvalue());price=upload('price','验收价格模型',asset)
 check('price_import_frozen_runtime',price['role']=='price');check_role_restart('price',price['id'])
 effect=read_json(package/release['effectiveness_model']);effect['model_version']=str(effect.get('model_version'))+'-library-check';effect_file=out/'effectiveness_bundle.json';write_json(effect_file,effect)
 effect_asset=upload('effectiveness','验收效能模型',effect_file);check('effect_import_frozen_runtime',effect_asset['role']=='effectiveness');check_role_restart('effectiveness',effect_asset['id'])
 active=call('/api/model-library')['active'];stop();start();check('both_model_choices_persist',call('/api/model-library')['active']==active)
 # Invalid assets must not change current service processes or selected IDs.
 before=status()['services'];invalid=out/'invalid.json';invalid.write_bytes(b'{}')
 try:upload('price','invalid',invalid);rejected=False
 except Exception:rejected=True
 check('bad_model_keeps_current',rejected and call('/api/model-library')['active']==active and status()['services']['price']['pid']==before['price']['pid'])
 check_role_restart('price','default');check('effect_choice_independent',call('/api/model-library')['active']['effectiveness']==effect_asset['id'])
 check_role_restart('effectiveness','default')
 check('reset_defaults',call('/api/model-library')['active']=={'price':'default','effectiveness':'default'})
 control('price','stop');control('web','restart')
 check('web_starts_without_price_service',call('/api/health')['status']=='ok' and not status()['services']['price']['alive'])
 control('price','start');check('price_service_recovers',status()['services']['price']['alive'])
 report['status']='pass'
except Exception as e:report.update(status='fail',error=repr(e));raise
finally:stop();write_json(out/'report.json',report)
print(json.dumps(report,ensure_ascii=True,indent=2))
