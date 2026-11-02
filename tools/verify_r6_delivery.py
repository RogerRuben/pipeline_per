"""Actual R6 EXE acceptance: metadata, protocols, scenarios and exported values."""
import argparse,csv,io,json,os,subprocess,sys,time
from pathlib import Path
from http.cookiejar import CookieJar
from urllib.request import build_opener,ProxyHandler,HTTPCookieProcessor,Request
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,verify_package
from product.launcher import running
from product.supervisor import fetch
from tools.client_builder import free_ports

def verify(package,out):
 package,out=Path(package).resolve(),Path(out).resolve()
 if out.exists() or ROOT/'outputs' not in out.parents:raise ValueError('Choose new project outputs')
 manifest=verify_package(package,True)
 if manifest['release']['classification']!='synthetic':raise ValueError('Automatic evidence uses synthetic data only')
 out.mkdir();data=out/'userdata';write_json(data/'config/desktop.json',free_ports())
 report={'checks':{},'source_sha':manifest['build_source_sha'],'package':str(package),'win7_validated':False}
 def check(name,value):
  report['checks'][name]=bool(value);write_json(out/'r6_acceptance.json',report)
  if not value:raise AssertionError(name)
 process=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data)],env=dict(os.environ,PATH=''))
 try:
  deadline=time.monotonic()+100
  while time.monotonic()<deadline:
   if process.poll() is not None:raise RuntimeError('EXE exited')
   live=running(data)
   if live and live[1]['status']=='ready':break
   time.sleep(.2)
  else:raise TimeoutError('EXE not ready')
  base=live[1]['services']['web']['url']+'/api/deep-price/v1'
  opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(CookieJar()))
  def call(path,payload=None,raw=False):
   request=Request(base+path,data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode(),headers={'Content-Type':'application/json'})
   with opener.open(request,timeout=45) as response:body=response.read()
   return body if raw else json.loads(body)
  schema=call('/schema');identity={k:schema[k] for k in ('product_code','release_id','model_version','price_model_sha256','model_instance_id','business_snapshot_sha256','price_output_signature')}
  products=[]
  page=1
  while True:
   response=call('/products?page='+str(page)+'&page_size=100');products.extend(response['items'])
   if len(products)>=response['total']:break
   if not response['items']:raise RuntimeError('Incomplete product pagination')
   page+=1
  params={f['field_id']:products[0]['parameters'].get(f['field_id']) for f in schema['fields']}
  importance=call('/feature-importance');value=importance['data']['feature_importance']
  check('published_importance_bound_to_model',importance['status']=='available' and value['model_sha256']==schema['price_model_sha256'])
  check('importance_reference_protocol',value['seed']==20261102 and value['repeats']==3 and bool(value['sample_ids']) and 'not_independent_test' in value['scope'])
  check('importance_ratios_and_std',all(r['std']>=0 for r in value['items']) and abs(sum(r['ratio'] for r in value['items'])-(1 if any(r['importance']>0 for r in value['items']) else 0))<1e-8)
  library=call('/model-library');item=library['items'][0];meta=item['metadata']
  check('readonly_single_published_model',library['installed_count']==1 and item['readonly'] and item['active'])
  check('metrics_have_explicit_units',meta['metric_units']['validation_metrics']['mape']=='ratio')
  check('active_members_use_actual_weights',len(meta['active_members'])==meta['metrics']['member_count'] and abs(sum(r['weight'] for r in meta['active_members'])-1)<1e-8)
  chosen=call('/sensitivity/top10',dict(identity,parameters=params,ranking_mode='importance',field_ids=None))
  expected=[r['field_id'] for r in value['items'] if any(f['field_id']==r['field_id'] and f['analysis_supported'] for f in schema['fields'])][:10]
  check('importance_top10_keeps_published_order',[r['field_id'] for r in chosen['series']]==expected and chosen['ranking_mode']=='importance')
  field=next(f for f in schema['fields'] if f['type']=='number' and isinstance(params[f['field_id']],(int,float)) and f['analysis_supported'])
  history=call('/sensitivity/single',dict(identity,parameters=params,field_id=field['field_id'],range_mode='historical'))
  observed=[r['parameters'][field['field_id']] for r in products if isinstance(r['parameters'].get(field['field_id']),(int,float))]
  low=max(min(observed),field.get('minimum',min(observed)));high=min(max(observed),field.get('maximum',max(observed)))
  points=history['series'][0]['points']
  check('historical_range_uses_published_values',abs(points[0]['value']-low)<1e-8 and abs(points[-1]['value']-high)<1e-8)
  alternative=dict(params);alternative[field['field_id']]=(low+high)/2
  scenario=call('/scenario',dict(identity,items=[{'item_id':'baseline','name':'基准','parameters':params},{'item_id':'changed','name':'改动','parameters':alternative}]))
  row=scenario['items'][1];original=scenario['items'][0]['result']['predicted_price_wan'];changed=row['result']['predicted_price_wan']
  check('scenario_server_delta_and_change_list',abs(row['delta_wan']-(changed-original))<1e-8 and bool(row['changed_fields']))
  txt=json.loads(call('/export',{'result_id':scenario['result_id'],'format':'txt'},True))
  contents=call('/export',{'result_id':scenario['result_id'],'format':'csv'},True)
  csv_values=dict(list(csv.reader(io.StringIO(contents.decode('utf-8-sig'))))[1:])
  check('csv_txt_match_native_results',abs(float(csv_values['items[1].delta_wan'])-txt['items'][1]['delta_wan'])<1e-8 and txt['price_model_sha256']==schema['price_model_sha256'])
  (out/'scenario.csv').write_bytes(contents);write_json(out/'scenario.json',txt)
  report.update(status='pass',active_members=meta['active_members'],sample_count=len(value['sample_ids']),model_sha256=schema['price_model_sha256'])
  write_json(out/'r6_acceptance.json',report);return report
 finally:
  live=running(data)
  if live and live[1]['pid']==process.pid:
   try:fetch(live[0]['url']+'/api/stop',{},live[0]['token'])
   except OSError:pass
  try:process.wait(timeout=30)
  except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)

if __name__=='__main__':
 parser=argparse.ArgumentParser();parser.add_argument('--package',required=True);parser.add_argument('--out',required=True);args=parser.parse_args()
 print(json.dumps(verify(args.package,args.out),ensure_ascii=False,indent=2))

