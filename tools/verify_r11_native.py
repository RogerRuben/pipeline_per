import argparse,os,subprocess,sys,time,socket
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import write_json,read_json,sha
from product.launcher import running,fetch
from tools.client_builder import free_ports
p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--cases',required=True);p.add_argument('--label',required=True);a=p.parse_args()
package=Path(a.package).resolve();out=ROOT/'outputs'/('r11_native_'+a.label);out.mkdir(exist_ok=False);data=out/'userdata';settings=free_ports();write_json(data/'config/desktop.json',settings);before=sha(data/'config/desktop.json');report={'checks':{},'win7_tested':False};process=None
block=socket.socket();block.bind(('127.0.0.1',settings['ports']['web']));block.listen()
def check(k,v):
 report['checks'][k]=bool(v);write_json(out/'report.json',report)
 if not v:raise AssertionError(k)
def start():
 global process
 process=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data)],creationflags=subprocess.CREATE_NO_WINDOW)
 for _ in range(300):
  current=running(data)
  if current and current[1]['status']=='ready':return current
  if process.poll() is not None:raise ValueError('EXE exited')
  time.sleep(.2)
 raise ValueError('readiness timeout')
def stop():
 current=running(data)
 if current:
  try:fetch(current[0]['url']+'/api/stop',{},current[0]['token'])
  except Exception:pass
 if process:process.wait(timeout=25)
try:
 current=start();base=current[1]['portal_url'][:-7];check('web_port_fallback_retained',current[1]['ports']['web']!=settings['ports']['web'])
 with (out/'browser.log').open('wb') as log:
  done=subprocess.run(['C:/Program Files/nodejs/node.exe',str(ROOT/'tools/verify_r11_ui.mjs'),base,current[0]['url'],str(out/'browser'),str(Path(a.cases).resolve())],stdout=log,stderr=subprocess.STDOUT,timeout=300)
 check('actual_browser_ui_and_all_model_cases',done.returncode==0)
 active=fetch(base+'/api/model-library')['active'];stop();current=start();base=current[1]['portal_url'][:-7]
 check('model_choices_persist_after_restart',fetch(base+'/api/model-library')['active']==active)
 check('preferences_not_overwritten',sha(data/'config/desktop.json')==before)
 check('portal_ready_after_restart',fetch(base+'/api/portal')['product']['product_code']==read_json(package/'PACKAGE_MANIFEST.json')['release']['product_code'])
 report['status']='pass'
finally:stop();block.close();write_json(out/'report.json',report)
print(report)
