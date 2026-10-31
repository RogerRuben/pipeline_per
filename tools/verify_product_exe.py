"""Acceptance against real frozen Windows processes in a relocated package.

Uses only new synthetic userdata. It never starts or stops a production instance.
"""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes as W
import csv
from http.cookiejar import CookieJar
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request,build_opener,ProxyHandler,HTTPCookieProcessor
import zipfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import read_json,sha,verify_package,write_json
from product.launcher import fetch,running


def wait_for(fn,seconds=90):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=fn()
        if result:return result
        time.sleep(.15)
    raise TimeoutError('Acceptance condition did not become true')


def process_handle(pid,expected_exe):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];kernel.OpenProcess.restype=W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,ctypes.POINTER(W.DWORD)]
    kernel.TerminateProcess.argtypes=[W.HANDLE,W.UINT]
    kernel.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD]
    kernel.CloseHandle.argtypes=[W.HANDLE]
    handle=kernel.OpenProcess(0x100001|0x1000,False,pid)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    name=ctypes.create_unicode_buffer(32768);size=W.DWORD(len(name))
    if not kernel.QueryFullProcessImageNameW(handle,0,name,ctypes.byref(size)) or Path(name.value).resolve()!=expected_exe.resolve():
        kernel.CloseHandle(handle)
        raise ValueError('Refuse to operate on an unexpected process image')
    return kernel,handle


def verify(package,upgrade,output):
    package,upgrade,output=Path(package).resolve(),Path(upgrade).resolve(),Path(output).resolve()
    if output.exists() or ROOT not in output.parents or output.parts[len(ROOT.parts)]!='outputs':
        raise ValueError('Choose a new project outputs directory')
    original=verify_package(package,True);new=verify_package(upgrade,True)
    if original['release']['classification']!='synthetic' or new['release']['classification']!='synthetic':
        raise ValueError('This destructive recovery test only accepts explicit synthetic packages')
    if original['version']==new['version']:raise ValueError('Upgrade acceptance requires distinct versions')
    output.mkdir(parents=True)
    relocated=output/'中文 空格路径/软件第一版'
    shutil.copytree(str(package),str(relocated))
    env=dict(os.environ,LOCALAPPDATA=str(output/'local_app_data'),PATH='')
    env.pop('PIPELINE_PER_DATA_DIR',None)
    for key in list(env):
        if key.startswith(('PYTHON','IPDEMO_','_PYI','_MEIPASS')):env.pop(key,None)
    data=output/'local_app_data/PipelinePer'
    report={'checks':{},'timings':{},'python_on_path':False,'classification':'synthetic',
            'win7_validated':False,'source_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=str(ROOT),text=True).strip(),
            'package_versions':[original['version'],new['version']],'model_sha256':original['files'][original['release']['price_model']]}
    def save():write_json(output/'acceptance.json',report)
    def check(name,value):
        report['checks'][name]=bool(value);save()
        if not value:raise AssertionError(name)
    def command(exe,*args):
        destination=output/('command-'+str(time.time_ns())+'.json')
        result=subprocess.run([str(exe),'--no-browser','--report',str(destination)]+list(args),
                              env=env,cwd=str(output),creationflags=subprocess.CREATE_NO_WINDOW,timeout=100)
        value=read_json(destination) if destination.exists() else {'missing_report':True}
        if result.returncode:raise RuntimeError(str(value))
        return value
    def launch(exe):
        started=time.monotonic()
        proc=subprocess.Popen([str(exe),'--no-browser'],env=env,cwd=str(output),creationflags=subprocess.CREATE_NO_WINDOW)
        def ready():
            if proc.poll() is not None:raise RuntimeError('EXE exited '+str(proc.returncode)+'; inspect '+str(data/'logs'))
            current=running(data)
            if current and current[1]['status']=='ready':return current
            return None
        try:
            current=wait_for(ready)
        except Exception:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)
            raise
        report['timings'].setdefault('cold_start_seconds',[]).append(round(time.monotonic()-started,3));save()
        return proc,current
    parent=None
    owned_handles=[]
    try:
        exe=relocated/'product.exe'
        raw=exe.read_bytes();offset=int.from_bytes(raw[60:64],'little')
        check('real_windowed_x64_pe',raw[:2]==b'MZ' and raw[offset:offset+4]==b'PE\0\0'
              and int.from_bytes(raw[offset+4:offset+6],'little')==0x8664
              and int.from_bytes(raw[offset+24+68:offset+24+70],'little')==2)
        check('frozen_package_integrity',command(exe,'--verify')['status']=='pass')
        parent,(control,status)=launch(exe)
        report['initial_status']=status;save()
        check('default_external_userdata',Path(status['data_dir'])==data and data not in relocated.parents)
        check('three_healthy_owned_processes',len(status['services'])==3 and all(x['alive'] for x in status['services'].values()))
        handles=[process_handle(v['pid'],relocated/'runtime/python38/pythonw.exe') for v in status['services'].values()]
        owned_handles.extend(handles)
        duplicate=command(exe)
        check('double_launch_reuses_instance',duplicate.get('reused_instance') and duplicate['pid']==parent.pid)
        web=status['services']['web']['url'];base=web+'/api/deep-price/v1'
        opener=build_opener(ProxyHandler({}),HTTPCookieProcessor(CookieJar()))
        def http(path,payload=None,raw=False):
            request=Request(web+path,data=None if payload is None else json.dumps(payload,ensure_ascii=False).encode('utf-8'),
                            headers={'Content-Type':'application/json'})
            with opener.open(request,timeout=45) as response:
                content=response.read()
                return content if raw else json.loads(content.decode('utf-8'))
        schema=http('/api/deep-price/v1/schema')
        products=http('/api/deep-price/v1/products')['items']
        fields={f['field_id'] for f in schema['fields']}
        from app.deep_price.features import IDENTITY
        identity={key:schema[key] for key in IDENTITY}
        parameters={k:v for k,v in products[0]['parameters'].items() if k in fields}
        request=dict(identity,parameters=parameters)
        prediction=http('/api/deep-price/v1/predict',request)
        simple=http('/api/price-workbench/predict',{'parameters':parameters})
        recommendation=http('/api/evaluate',{'parameters':parameters})
        report['prediction_values']={'deep':prediction['predicted_price_wan'],
            'simple':simple['prediction']['predicted_price_wan'],
            'recommendation':recommendation['predicted_price_wan']}
        delta=max(report['prediction_values'].values())-min(report['prediction_values'].values())
        check('native_v3_http_consistency',delta<1e-8 and schema['price_model_sha256']==report['model_sha256'])
        check('native_sixty_fields',len(schema['fields'])==60)
        before=time.monotonic()
        top=http('/api/deep-price/v1/sensitivity/top10',dict(request,field_ids=None))
        report['timings']['top10_seconds']=round(time.monotonic()-before,3)
        report['top10']={key:top.get(key) for key in ('sample_count','batch_count','compared_field_count')}
        check('top10_under_budget',top['compared_field_count']==60 and report['timings']['top10_seconds']<30)
        exported=http('/api/deep-price/v1/export',{'result_id':prediction['result_id'],'format':'csv'},True)
        (output/'prediction.csv').write_bytes(exported)
        rows=list(csv.reader(io.StringIO(exported.decode('utf-8-sig'))))
        check('csv_contains_service_price',any(str(prediction['predicted_price_wan']) in row for row in rows))
        check('unconfigured_correction_honest',http('/api/deep-price/v1/adjustment/status')['status']=='not_configured')
        check('portal_links_include_control',any(item['key']=='desktop_control' for item in http('/api/portal')['services']))
        database=data/'data/protocol_demo.db'
        with closing(sqlite3.connect(str(database))) as connection:
            connection.execute('CREATE TABLE p4_acceptance_sentinel(value TEXT)')
            connection.execute('INSERT INTO p4_acceptance_sentinel VALUES (?)',('保留用户方案',));connection.commit()
        rule=data/'config/deep_price_adjustment.json'
        write_json(rule,{'confirmed':False,'user_note':'重新打包后必须保留'})
        rule_sha=sha(rule)
        def preserved():
            with closing(sqlite3.connect(str(database))) as connection:
                value=connection.execute('SELECT value FROM p4_acceptance_sentinel').fetchone()[0]
                healthy=connection.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            return value=='保留用户方案' and sha(rule)==rule_sha and healthy
        check('online_backup',Path(command(exe,'--backup')['backup']).is_file())
        old_generation=status['generation'];failed_price=status['services']['price']['pid']
        kernel,handle=process_handle(failed_price,relocated/'runtime/python38/pythonw.exe')
        try:kernel.TerminateProcess(handle,97)
        finally:kernel.CloseHandle(handle)
        started=time.monotonic()
        recovered=wait_for(lambda:(lambda x:x if x and x[1]['status']=='ready' and x[1]['generation']!=old_generation else None)(running(data)))
        report['timings']['worker_recovery_seconds']=round(time.monotonic()-started,3)
        check('automatic_worker_recovery',recovered[1]['restart_count']==1 and preserved())
        before_generation=recovered[1]['generation']
        command(exe,'--restart')
        wait_for(lambda:(lambda x:x if x and x[1]['status']=='ready' and x[1]['generation']!=before_generation else None)(running(data)))
        check('manual_restart_preserves_data',preserved())
        crash_status=running(data)[1]
        crash_handles=[process_handle(v['pid'],relocated/'runtime/python38/pythonw.exe') for v in crash_status['services'].values()]
        owned_handles.extend(crash_handles)
        parent.kill();parent.wait(timeout=10);parent=None
        check('supervisor_crash_leaves_no_workers',all(k.WaitForSingleObject(h,10000)==0 for k,h in crash_handles))
        parent,(control,status)=launch(exe)
        check('stale_control_recovers_without_reset',preserved())
        command(exe,'--stop');parent.wait(timeout=20);parent=None
        check('clean_stop_removes_control',not (data/'runtime/control.json').exists())
        upgrade_copy=output/'中文 空格路径/软件第二版'
        shutil.copytree(str(upgrade),str(upgrade_copy))
        before_backups=set((data/'backups').glob('backup-*.zip'))
        parent,(control,status)=launch(upgrade_copy/'product.exe')
        check('repack_update_preserves_user_data',preserved() and status['version']==new['version'])
        check('update_takes_preload_backup',bool(set((data/'backups').glob('backup-*.zip'))-before_backups))
        command(upgrade_copy/'product.exe','--stop');parent.wait(timeout=20);parent=None
        check('packages_immutable_during_execution',verify_package(relocated,True) and verify_package(upgrade_copy,True))
        report['status']='pass';save()
        return report
    except Exception as exc:
        report.update(status='fail',error=str(exc));save();raise
    finally:
        if parent is not None and parent.poll() is None:
            current=running(data)
            if current:
                try:fetch(current[0]['url']+'/api/stop',{},current[0]['token'])
                except OSError:pass
            try:parent.wait(timeout=15)
            except subprocess.TimeoutExpired:parent.kill();parent.wait(timeout=10)
        for kernel,handle in owned_handles:kernel.CloseHandle(handle)


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--package',required=True);p.add_argument('--upgrade-package',required=True);p.add_argument('--out',required=True);a=p.parse_args()
    print(json.dumps(verify(a.package,a.upgrade_package,a.out),ensure_ascii=True,indent=2))
