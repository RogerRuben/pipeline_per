import argparse,json,subprocess,sys,time,base64,io,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha
from product.launcher import running,fetch
from tools.client_builder import free_ports
p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--export',required=True);p.add_argument('--expected',required=True);p.add_argument('--label',required=True);p.add_argument('--preloaded',action='store_true');a=p.parse_args()
package=Path(a.package).resolve();export=Path(a.export).resolve();expected=read_json(a.expected);out=ROOT/'outputs'/('r12_native_'+a.label);out.mkdir(exist_ok=False);data=out/'userdata';settings=free_ports();write_json(data/'config/desktop.json',settings);process=None;report={'checks':{},'win7_tested':False}
def check(k,v):
 report['checks'][k]=bool(v);write_json(out/'report.json',report)
 if not v:raise AssertionError(k)
def start():
 global process,control,base
 process=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data)],creationflags=subprocess.CREATE_NO_WINDOW)
 for _ in range(400):
  current=running(data)
  if current and current[1]['status']=='ready':control=current[0];base=current[1]['portal_url'][:-7];return
  if process.poll() is not None:raise ValueError('EXE exited')
  time.sleep(.2)
 raise ValueError('Readiness timeout')
def stop():
 current=running(data)
 if current:
  try:fetch(current[0]['url']+'/api/stop',{},current[0]['token'])
  except Exception:pass
 if process:process.wait(timeout=25)
def call(path,body=None):return fetch(base+path,body,timeout=100)
def status():return fetch(control['url']+'/api/status')
try:
 start();check('initial_ready',True)
 if not a.preloaded:
  before=status()['services'];config=sha(data/'config/desktop.json')
  with (out/'browser.log').open('wb') as log:
   done=subprocess.run(['C:/Program Files/nodejs/node.exe',str(ROOT/'tools/verify_expert_ui.mjs'),base,str(export),str(out)],stdout=log,stderr=subprocess.STDOUT,timeout=180)
  check('original_zip_import_and_activate_in_browser',done.returncode==0)
  after=status()['services'];check('only_effect_worker_restarted',before['price']['pid']==after['price']['pid'] and before['web']['pid']==after['web']['pid'] and before['effectiveness']['pid']!=after['effectiveness']['pid'])
  check('expert_binary_is_running',str((data/'runtime/status.json').exists())=='True' and after['effectiveness']['alive'])
  library=call('/api/model-library');asset=next(i for i in library['items'] if i['active']);folder=data/'models/library'/asset['id'];check('installed_asset_has_no_source',not list(folder.rglob('*.py')) and not list(folder.rglob('*.xlsx')))
  with zipfile.ZipFile(io.BytesIO(fetch_bytes:=__import__('urllib.request',fromlist=['urlopen']).urlopen(base+'/api/model-library/export/'+asset['id']).read())) as z:check('library_reexport_is_data_only',all(not n.endswith('.py') for n in z.namelist()))
 schema=call('/api/effectiveness-workbench/schema');check('original_pump_backend',schema['backend']=='original_pump_v13' and schema['profile_version']==13)
 history=call('/api/deep-price/v1/products?page=1&page_size=1')['items'][0]['parameters'];price_before=call('/api/price-workbench/predict',{'parameters':history})['prediction']['predicted_price_wan'];rows=[]
 for index,case in enumerate(expected['cases']):
  params=dict(history,**case['parameters']);value=call('/api/effectiveness-workbench/evaluate',{'parameters':params,'target_protocol':case['target_protocol']})
  score=value['evaluation']['effectiveness_score'];rows.append({'expected':case['score'],'actual':score,'task':case['target_protocol']});report['scores']=rows;check('expert_score_'+str(index),abs(score-case['score'])<1e-6)
  check('raw_expert_result_'+str(index),abs(value['raw_expert_result']['effectiveness_score']-case['score'])<1e-6)
  normalized=call('/api/evaluate',{'parameters':params,'target_protocol':case['target_protocol']});check('recommendation_score_'+str(index),abs(normalized['capability_score']-score)<1e-6)
 report['scores']=rows
 check('price_model_unchanged',abs(call('/api/price-workbench/predict',{'parameters':history})['prediction']['predicted_price_wan']-price_before)<1e-8)
 active=call('/api/model-library')['active'];stop();start();check('active_model_persists',call('/api/model-library')['active']==active);check('expert_loads_after_restart',call('/api/effectiveness-workbench/schema')['backend']=='original_pump_v13')
 if not a.preloaded:
  call('/api/model-library/activate',{'role':'effectiveness','id':'default'});check('can_restore_trial_backend',call('/api/effectiveness-workbench/schema')['backend']=='snapshot_json');check('preferences_unchanged',sha(data/'config/desktop.json')==config)
 report['status']='pass'
finally:stop();write_json(out/'report.json',report)
print(report)
