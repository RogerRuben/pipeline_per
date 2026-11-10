"""Actual EXE occupied-port acceptance; never stops a foreign listener."""
import argparse,json,socket,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha
from product.launcher import running,fetch
from tools.client_builder import free_ports
p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--label',required=True);p.add_argument('--short',action='store_true');p.add_argument('--browser',action='store_true');a=p.parse_args()
package=Path(a.package).resolve();out=ROOT/'outputs'/('r10_ports_'+a.label);out.mkdir(exist_ok=False)
report={'checks':{},'cases':{},'win7_tested':False}
def check(k,v):
 report['checks'][k]=bool(v);write_json(out/'report.json',report)
 if not v:raise AssertionError(k)
def hold(port):
 s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',port));s.listen();return s
for case,blocked in ([('all',('control','web','price','effectiveness'))] if a.short else [('none',()),('web',('web',)),('price',('price',)),('effectiveness',('effectiveness',)),('control',('control',)),('all',('control','web','price','effectiveness'))]):
 data=out/case;settings=free_ports();write_json(data/'config/desktop.json',settings);original=sha(data/'config/desktop.json');listeners=[];process=None
 try:
  listeners=[hold(settings['ports'][r]) for r in blocked]
  process=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data)],creationflags=subprocess.CREATE_NO_WINDOW)
  current=None
  for _ in range(300):
   if process.poll() is not None:raise AssertionError('EXE exited: '+case)
   current=running(data)
   if current and current[1]['status']=='ready':break
   time.sleep(.2)
  else:raise AssertionError('Readiness timeout: '+case)
  def status():return fetch(current[0]['url']+'/api/status')
  def control(role,action):
   op=fetch(current[0]['url']+'/api/services/'+role+'/'+action,{},current[0]['token'])
   for _ in range(300):
    s=status();operation=next(o for o in s['operations'] if o['id']==op['operation_id'])
    if operation['status']=='succeeded':return s
    if operation['status']=='failed':raise AssertionError(operation.get('error'))
    time.sleep(.2)
   raise AssertionError('Operation timeout')
  def smoke(s):
   base=s['portal_url'].removesuffix('/portal') if hasattr(str,'removesuffix') else s['portal_url'][:-7]
   schema=fetch(base+'/api/deep-price/v1/schema');params=fetch(base+'/api/deep-price/v1/products?page=1&page_size=1')['items'][0]['parameters']
   identity={k:schema[k] for k in ('product_code','release_id','model_version','price_model_sha256','model_instance_id','business_snapshot_sha256','price_output_signature')}
   deep=fetch(base+'/api/deep-price/v1/predict',dict(identity,parameters=params))['predicted_price_wan']
   simple=fetch(base+'/api/price-workbench/predict',{'parameters':params})['prediction']['predicted_price_wan']
   reco=fetch(base+'/api/evaluate',{'parameters':params})['predicted_price_wan']
   schemes=fetch(base+'/cost-effectiveness/api/schemes')['items'][:2]
   cost=fetch(base+'/cost-effectiveness/api/analyze',{'scheme_ids':[v['scheme_id'] for v in schemes]})
   return abs(deep-simple)<1e-8 and abs(deep-reco)<1e-8 and cost['success']
  s=status();check(case+'_all_services_running',all(v['alive'] for v in s['services'].values()))
  check(case+'_only_occupied_ports_changed',all((s['ports'][r]!=settings['ports'][r])==(r in blocked) for r in settings['ports']))
  check(case+'_live_api_consistency',smoke(s));check(case+'_foreign_listeners_preserved',all(l.fileno()>=0 for l in listeners))
  check(case+'_config_unchanged',sha(data/'config/desktop.json')==original)
  addresses=read_json(data/'runtime/addresses.json');check(case+'_published_actual_addresses',addresses['portal']==s['portal_url'] and addresses['management']==s['control_url'])
  report['cases'][case]={'configured':settings['ports'],'actual':s['ports']}
  if a.browser:
   done=subprocess.run(['C:/Program Files/nodejs/node.exe',str(ROOT/'tools/verify_port_ui.mjs'),s['control_url'],s['portal_url'],str(settings['ports']['web']),str(out)],capture_output=True,timeout=90)
   (out/'browser.log').write_bytes(done.stdout+done.stderr);check(case+'_actual_browser_addresses',done.returncode==0)
  if case=='none':
   for role in ('price','effectiveness','web'):
    before=status();old=before['ports'][role];control(role,'stop');listeners.append(hold(old));after=control(role,'start')
    check(role+'_restart_moves_occupied_port',after['ports'][role]!=old)
    check(role+'_restart_preserves_other_processes',all(before['services'][r]['pid']==after['services'][r]['pid'] for r in before['services'] if r!=role))
    check(role+'_restart_updates_all_clients',smoke(after))
  check(case+'_final_config_unchanged',sha(data/'config/desktop.json')==original)
 finally:
  current=running(data)
  if current:
   try:fetch(current[0]['url']+'/api/stop',{},current[0]['token'])
   except Exception:pass
  if process:
   try:process.wait(timeout=25)
   except subprocess.TimeoutExpired:process.kill();process.wait()
  for listener in listeners:listener.close()
report['status']='pass';write_json(out/'report.json',report);print(json.dumps(report,ensure_ascii=True,indent=2))
