"""Portable target-machine exercise using only the bundled interpreter.

Creates new isolated userdata, never touches an existing installation or uploads
reports. Win7 executable checks are separate from manual browser acceptance.
"""
import argparse
from contextlib import closing
import ctypes
from ctypes import wintypes as W
import json
import os
from pathlib import Path
import platform
import sqlite3
import subprocess
import sys
import time
from urllib.request import Request,build_opener,ProxyHandler

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,verify_package,sha
from product.launcher import running,fetch


def wait_for(fn,seconds=100):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        result=fn()
        if result:return result
        time.sleep(.2)
    raise TimeoutError('Target-machine condition timed out')


def owned_handle(pid,exe):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[W.DWORD,W.BOOL,W.DWORD];kernel.OpenProcess.restype=W.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes=[W.HANDLE,W.DWORD,W.LPWSTR,ctypes.POINTER(W.DWORD)]
    kernel.WaitForSingleObject.argtypes=[W.HANDLE,W.DWORD];kernel.CloseHandle.argtypes=[W.HANDLE]
    kernel.TerminateProcess.argtypes=[W.HANDLE,W.UINT]
    handle=kernel.OpenProcess(0x100001|0x1000,False,pid)
    if not handle:raise ctypes.WinError(ctypes.get_last_error())
    buffer=ctypes.create_unicode_buffer(32768);size=W.DWORD(len(buffer))
    if not kernel.QueryFullProcessImageNameW(handle,0,buffer,ctypes.byref(size)) or Path(buffer.value).resolve()!=exe.resolve():
        kernel.CloseHandle(handle);raise ValueError('Unexpected worker image; refusing to terminate it')
    return kernel,handle


def exercise(package,out,rollback_package=None):
    package,out=Path(package).resolve(),Path(out).resolve()
    if out.exists() or out==package or package in out.parents or out in package.parents:
        raise ValueError('Choose a new evidence directory outside the package')
    out.mkdir(parents=True)
    win=sys.getwindowsversion()
    target_win7=(win.major,win.minor,win.product_type)==(6,1,1) and win.service_pack_major>=1
    report={'format':'pipeline-target-exercise-1.0','host':platform.platform(),'python':sys.version,
            'os_version':list(win[:5]),'win7_sp1_workstation':target_win7,'checks':{},'timings':{},
            'win7_validated':False,'manual_browser_acceptance':'pending','upload_performed':False}
    def save():write_json(out/'target_report.json',report)
    def check(key,value):
        report['checks'][key]=bool(value);save()
        if not value:raise AssertionError(key)
    manifest=verify_package(package,True)
    report.update(package_version=manifest['version'],build_source_sha=manifest['build_source_sha'],
                  classification=manifest['release']['classification'],model_sha256=manifest['files'][manifest['release']['price_model']])
    save()
    if report['classification']!='synthetic':
        raise ValueError('Automatic crash/restore exercise requires a synthetic package; formal data needs a separate plan')
    env=dict(os.environ,PATH='')
    for key in list(env):
        if key.startswith(('PYTHON','IPDEMO_','_PYI','_MEIPASS')):env.pop(key,None)
    parent=None;data=out/'userdata';handles=[]
    def command(folder,user,*args,success=True):
        path=out/('command-%d.json'%time.time_ns())
        result=subprocess.run([str(folder/'product.exe'),'--no-browser','--data-dir',str(user),'--report',str(path)]+list(args),
                              env=env,cwd=str(out),creationflags=subprocess.CREATE_NO_WINDOW,timeout=120)
        value=read_json(path) if path.exists() else {'status':'missing_report','exit_code':result.returncode}
        if success and result.returncode:raise RuntimeError(str(value))
        return result.returncode,value
    def start(folder,user):
        started=time.monotonic()
        process=subprocess.Popen([str(folder/'product.exe'),'--no-browser','--data-dir',str(user)],
                                 env=env,cwd=str(out),creationflags=subprocess.CREATE_NO_WINDOW)
        def ready():
            if process.poll() is not None:raise RuntimeError('EXE exited; inspect '+str(user/'logs/launcher.log'))
            item=running(user)
            return item if item and item[1]['status']=='ready' else None
        try:item=wait_for(ready)
        except Exception:
            if process.poll() is None:process.kill();process.wait(timeout=15)
            raise
        report['timings'].setdefault('startup_seconds',[]).append(round(time.monotonic()-started,3));save()
        return process,item[1]
    def http(status,path,payload=None):
        request=Request(status['services']['web']['url']+path,
                        data=None if payload is None else json.dumps(payload).encode('utf-8'),
                        headers={'Content-Type':'application/json'})
        with build_opener(ProxyHandler({})).open(request,timeout=45) as response:return json.load(response)
    def stop(folder,user):
        nonlocal parent
        command(folder,user,'--stop');parent.wait(timeout=25);parent=None
    try:
        check('frozen_verify',command(package,data,'--verify')[1]['status']=='pass')
        parent,status=start(package,data)
        check('three_services',len(status['services'])==3 and all(v['alive'] for v in status['services'].values()))
        check('empty_path_start',True)
        check('duplicate_launch',command(package,data)[1]['pid']==parent.pid)
        schema=http(status,'/api/deep-price/v1/schema')
        products=http(status,'/api/deep-price/v1/products')['items']
        fields={field['field_id'] for field in schema['fields']}
        parameters={key:value for key,value in products[0]['parameters'].items() if key in fields}
        identity={key:schema[key] for key in ('product_code','release_id','model_version','price_model_sha256',
                 'model_instance_id','business_snapshot_sha256','price_output_signature')}
        request=dict(identity,parameters=parameters)
        deep=http(status,'/api/deep-price/v1/predict',request)
        simple=http(status,'/api/price-workbench/predict',{'parameters':parameters})
        evaluation=http(status,'/api/evaluate',{'parameters':parameters})
        values=[deep['predicted_price_wan'],simple['prediction']['predicted_price_wan'],evaluation['predicted_price_wan']]
        report['prices_wan']=values
        check('native_v3_consistency',max(values)-min(values)<1e-8 and schema['price_model_sha256']==report['model_sha256'])
        started=time.monotonic();top=http(status,'/api/deep-price/v1/sensitivity/top10',dict(request,field_ids=None))
        report['timings']['top10_seconds']=round(time.monotonic()-started,3)
        check('top10_under_30_seconds',report['timings']['top10_seconds']<30 and top['compared_field_count']==len(fields))
        saved=http(status,'/api/save-scheme',{'scheme_name':'目标机备份恢复验收方案','parameters':parameters,
                   'evaluation_token':evaluation['evaluation_token'],'risk_confirmed':True,'base_agreement_id':products[0]['product_id']})
        check('real_saved_scheme_created',saved.get('saved') is True)
        saved_before=http(status,'/api/saved')['items']
        report['saved_ids']=[item['id'] for item in saved_before]
        rule=data/'config/deep_price_adjustment.json'
        write_json(rule,{'confirmed':False,'restore_acceptance_note':'独立恢复后保留'})
        rule_sha=sha(rule)
        _,backup=command(package,data,'--backup');backup_path=Path(backup['backup'])
        check('backup_created',backup_path.is_file())
        # Only current owned process handles are used; never image-wide taskkill.
        k,h=owned_handle(status['services']['price']['pid'],package/'runtime/python38/pythonw.exe')
        try:k.TerminateProcess(h,98)
        finally:k.CloseHandle(h)
        generation=status['generation']
        status=wait_for(lambda:(lambda item:item[1] if item and item[1]['status']=='ready' and item[1]['generation']!=generation else None)(running(data)))
        check('worker_exit_recovery',status['restart_count']>=1)
        handles=[owned_handle(item['pid'],package/'runtime/python38/pythonw.exe') for item in status['services'].values()]
        parent.kill();parent.wait(timeout=15);parent=None
        check('parent_crash_cleans_all_workers',all(k.WaitForSingleObject(h,10000)==0 for k,h in handles))
        for k,h in handles:k.CloseHandle(h)
        handles=[]
        parent,status=start(package,data)
        check('stale_instance_recovers',http(status,'/api/saved')['items']==saved_before)
        stop(package,data)
        restored=out/'restored-userdata'
        _,restoration=command(package,restored,'--restore',str(backup_path))
        check('offline_restore_command',restoration.get('status')=='restored')
        parent,status=start(package,restored)
        check('restored_saved_schemes_match',http(status,'/api/saved')['items']==saved_before)
        check('restored_rule_bytes_match',sha(restored/'config/deep_price_adjustment.json')==rule_sha)
        restored_schema=http(status,'/api/deep-price/v1/schema')
        fresh={key:restored_schema[key] for key in identity}
        restored_prediction=http(status,'/api/deep-price/v1/predict',dict(fresh,parameters=parameters))
        check('restored_prediction_matches',restored_prediction['predicted_price_wan']==deep['predicted_price_wan'])
        with closing(sqlite3.connect(str(restored/'data/protocol_demo.db'))) as conn:
            check('restored_sqlite_integrity',conn.execute('PRAGMA integrity_check').fetchone()[0]=='ok')
        stop(package,restored)
        before=sha(restored/'data/protocol_demo.db')
        code,error=command(package,restored,'--restore',str(backup_path),success=False)
        check('restore_never_overwrites_existing_data',code!=0 and sha(restored/'data/protocol_demo.db')==before)
        if rollback_package:
            rollback=Path(rollback_package).resolve();old=verify_package(rollback,True)
            if old['files'][old['release']['price_model']]!=report['model_sha256']:
                raise ValueError('Rollback exercise requires a matching predecessor model')
            parent,status=start(rollback,restored)
            check('rollback_package_starts_restored_data',http(status,'/api/saved')['items']==saved_before)
            stop(rollback,restored)
            parent,status=start(package,restored)
            check('return_to_current_after_rollback',http(status,'/api/saved')['items']==saved_before)
            stop(package,restored)
        check('delivery_files_unchanged',verify_package(package,True) is not None)
        report.update(status='pass',win7_executable_checks_passed=target_win7,
                      gate='pending_manual_browser_acceptance' if target_win7 else 'not_a_win7_target_machine')
        save();return report
    except Exception as exc:
        report.update(status='fail',error=str(exc));save();raise
    finally:
        if parent is not None and parent.poll() is None:
            parent.kill();parent.wait(timeout=15)
        for k,h in handles:k.CloseHandle(h)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--package',required=True);parser.add_argument('--out',required=True)
    parser.add_argument('--rollback-package');args=parser.parse_args()
    result=exercise(args.package,args.out,args.rollback_package)
    print(json.dumps(result,ensure_ascii=True,indent=2))
