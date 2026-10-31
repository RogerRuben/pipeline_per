"""Real EXE verification of external ports and independent service operations."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,sha,verify_package
from product.launcher import running
from product.supervisor import fetch
from tools.client_builder import free_ports
from tools.source_provenance import source_commit


def wait_for(fn,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=fn()
        if result:return result
        time.sleep(.15)
    raise TimeoutError('Service operation did not complete')


def verify(package,out):
    package,out=Path(package).resolve(),Path(out).resolve()
    if out.exists() or ROOT not in out.parents or out.parts[len(ROOT.parts)]!='outputs':raise ValueError('Choose new project outputs')
    manifest=verify_package(package,True)
    if manifest['release']['classification']!='synthetic':raise ValueError('Use synthetic data for process-control acceptance')
    out.mkdir(parents=True);data=out/'userdata';cfg=free_ports();write_json(data/'config/desktop.json',cfg)
    report={'checks':{},'configured_ports':cfg['ports'],'source_sha':source_commit(ROOT),'win7_validated':False}
    parent=None;env=dict(os.environ,PATH='')
    def check(name,value):
        report['checks'][name]=bool(value);write_json(out/'controls.json',report)
        if not value:raise AssertionError(name)
    def start():
        process=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data)],
                                 env=env,cwd=str(out),creationflags=subprocess.CREATE_NO_WINDOW)
        def ready():
            if process.poll() is not None:raise RuntimeError('EXE startup failed; see '+str(data/'logs'))
            value=running(data);return value if value and value[1]['status']=='ready' else None
        try:value=wait_for(ready)
        except Exception:
            if process.poll() is None:process.kill();process.wait(timeout=10)
            raise
        return process,value
    def operation(control,role,action):
        response=fetch(control['url']+'/api/services/'+role+'/'+action,{},control['token'])
        def done():
            current=running(data)
            if not current:return None
            item=next((item for item in current[1]['operations'] if item['id']==response['operation_id']),None)
            if item and item['status']=='failed':raise RuntimeError(item.get('error'))
            return current[1] if item and item['status']=='succeeded' else None
        return wait_for(done)
    try:
        parent,(control,status)=start()
        check('uses_external_ports',status['ports']==cfg['ports'])
        check('native_launcher_pid',status['pid']==parent.pid and status['supervisor_pid']!=parent.pid)
        check('address_record_without_browser',(data/'runtime/access.txt').is_file() and
              str(cfg['ports']['web']) in (data/'runtime/access.txt').read_text(encoding='utf-8'))
        original={role:item['pid'] for role,item in status['services'].items()}
        status=operation(control,'price','stop')
        check('price_stop_keeps_other_processes',not status['services']['price']['alive'] and status['services']['price']['paused']
              and all(status['services'][role]['pid']==original[role] for role in ('web','effectiveness')))
        time.sleep(6)
        check('intentional_stop_not_auto_restarted',not running(data)[1]['services']['price']['alive'])
        proxy=fetch(status['services']['web']['url']+'/api/desktop-status')
        check('portal_receives_real_status',proxy['services']['price']['paused'] and proxy['business']['historical_count']>0)
        status=operation(control,'price','start')
        check('price_start_same_address',status['ports']['price']==cfg['ports']['price'] and status['services']['price']['pid']!=original['price'])
        first_price=status['services']['price']['pid'];status=operation(control,'price','restart')
        check('price_restart_is_independent',status['services']['price']['pid']!=first_price and
              all(status['services'][role]['pid']==original[role] for role in ('web','effectiveness')))
        before={role:item['pid'] for role,item in status['services'].items()}
        status=operation(control,'effectiveness','stop');status=operation(control,'effectiveness','start')
        check('effect_service_independent',status['services']['effectiveness']['pid']!=before['effectiveness'] and
              all(status['services'][role]['pid']==before[role] for role in ('web','price')))
        before={role:item['pid'] for role,item in status['services'].items()}
        status=operation(control,'web','stop')
        check('web_stop_keeps_management',status['status']=='degraded' and not status['services']['web']['alive'])
        status=operation(control,'web','start')
        check('web_start_independent_and_stable',status['ports']['web']==cfg['ports']['web'] and
              all(status['services'][role]['pid']==before[role] for role in ('price','effectiveness')))
        fetch(control['url']+'/api/stop',{},control['token']);parent.wait(timeout=20);parent=None
        held=socket.socket();held.bind(('127.0.0.1',0));held.listen(1)
        bad=json.loads(json.dumps(cfg));bad['ports']['control']=held.getsockname()[1];write_json(data/'config/desktop.json',bad)
        error_file=out/'occupied-port.json'
        blocked=subprocess.Popen([str(package/'product.exe'),'--no-browser','--data-dir',str(data),'--report',str(error_file)],
                                 env=env,cwd=str(out),creationflags=subprocess.CREATE_NO_WINDOW)
        try:code=blocked.wait(timeout=60)
        finally:
            held.close()
            if blocked.poll() is None:blocked.kill();blocked.wait(timeout=10)
        error=read_json(error_file)
        check('occupied_port_rejected_without_silent_fallback',code!=0 and '管理端口' in error.get('error',''))
        write_json(data/'config/desktop.json',cfg)
        parent,(control,status)=start()
        check('external_config_recovery',status['ports']==cfg['ports'])
        fetch(control['url']+'/api/stop',{},control['token']);parent.wait(timeout=20);parent=None
        check('package_immutable',verify_package(package,True) is not None)
        report.update(status='pass');write_json(out/'controls.json',report);return report
    except Exception as exc:
        report.update(status='fail',error=str(exc));write_json(out/'controls.json',report);raise
    finally:
        if parent is not None and parent.poll() is None:
            item=running(data)
            if item:
                try:fetch(item[0]['url']+'/api/stop',{},item[0]['token'])
                except OSError:pass
            try:parent.wait(timeout=15)
            except subprocess.TimeoutExpired:parent.kill();parent.wait(timeout=10)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    print(json.dumps(verify(a.package,a.out),ensure_ascii=True,indent=2))
