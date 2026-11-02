"""Frozen, windowless desktop supervisor; workers use a private model runtime."""
import argparse
from collections import deque
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time
from urllib.parse import urlparse
from urllib.request import Request,build_opener,ProxyHandler
import webbrowser

from product.common import InstanceLock,data_directory,read_json,verify_package,write_json
from product.windows import WorkerJob
from product.compatibility import validate_data,check_data_root
from product.recovery import backup_data,restore_backup

HTTP = build_opener(ProxyHandler({}))  # Loopback never goes through a system proxy.


def fetch(url, payload=None, token=None, timeout=3):
    headers = {'Content-Type':'application/json'}
    if token:
        headers['X-Product-Token'] = token
    data = None if payload is None else json.dumps(payload).encode('utf-8')
    with HTTP.open(Request(url,data=data,headers=headers),timeout=timeout) as response:
        return json.load(response)


def running(data):
    try:
        control = read_json(data/'runtime/control.json')
        parsed = urlparse(control['url'])
        if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.path not in ('','/'):
            return None
        status = fetch(control['url']+'/api/status')
        if status['instance_id'] == control['instance_id']:
            return control,status
    except (OSError,ValueError,KeyError):
        pass
    return None


from product.supervisor import Supervisor


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--report',help='Write command result as UTF-8 JSON (windowless EXE)')
    parser.add_argument('--package-root',help=argparse.SUPPRESS)
    parser.add_argument('--launcher-pid',type=int,help=argparse.SUPPRESS)
    parser.add_argument('--service',choices=['price','effectiveness','web'])
    group = parser.add_mutually_exclusive_group()
    for command in ('stop','start','status','restart','backup','verify','addresses','open'):
        group.add_argument('--'+command,action='store_true')
    group.add_argument('--restore',metavar='BACKUP_ZIP',help='Offline restore into a new --data-dir; never overwrite userdata')
    args = parser.parse_args(argv)
    package = Path(args.package_root).resolve() if args.package_root else Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
    data = data_directory(package,args.data_dir)
    def report(value):
        if args.report:
            write_json(args.report,value)
    if args.verify:
        manifest = verify_package(package,full=True)
        report({'status':'pass','files_verified':len(manifest['files']),'version':manifest['version']})
        return 0
    if args.restore:
        manifest=verify_package(package,full=True)
        report(restore_backup(package,data,manifest,args.restore))
        return 0
    existing = running(data)
    if args.addresses:
        from product.desktop_config import load_config
        settings=load_config(package,data)
        value=existing[1] if existing else {'status':'stopped','ports':settings['ports'],
                'portal_url':'http://127.0.0.1:%d/portal'%settings['ports']['web'],
                'control_url':'http://127.0.0.1:%d/'%settings['ports']['control']}
        print(json.dumps(value,ensure_ascii=False,indent=2));report(value);return 0
    if args.open:
        if not existing:raise RuntimeError('软件尚未启动，请先运行 product.exe。')
        webbrowser.open(existing[1]['portal_url'] if existing[1]['services']['web']['alive'] else existing[0]['url'])
        report(existing[1]);return 0
    if args.service and not any((args.stop,args.start,args.restart)):raise ValueError('--service requires --start, --stop or --restart')
    if args.start and not args.service:raise ValueError('--start requires --service')
    if any((args.stop,args.start,args.status,args.restart,args.backup)):
        if not existing:
            report({'status':'stopped'})
            return 0 if args.stop or args.status else 1
        control,status = existing
        command = 'stop' if args.stop else 'start' if args.start else 'restart' if args.restart else 'backup' if args.backup else None
        path='/api/services/'+args.service+'/'+command if args.service else '/api/'+str(command)
        report(fetch(control['url']+path,{},control['token']) if command else status)
        return 0
    data.mkdir(parents=True,exist_ok=True)
    try:
        lock = InstanceLock(data)
    except RuntimeError:
        # A double click during cold start waits for the same instance.
        deadline = time.monotonic()+75
        while time.monotonic() < deadline:
            existing = running(data)
            if existing and existing[1]['status'] in ('ready','degraded','failed'):
                status = existing[1]
                if not args.no_browser:
                    webbrowser.open(status['portal_url'] if status['status']=='ready' else existing[0]['url'])
                report(dict(status,reused_instance=True))
                return 0
            time.sleep(.2)
        raise RuntimeError('Another instance is starting; check user-data logs')
    try:
        (data/'logs').mkdir(exist_ok=True)
        handler = RotatingFileHandler(str(data/'logs/launcher.log'),maxBytes=2*1024*1024,backupCount=3,encoding='utf-8')
        logging.basicConfig(level=logging.INFO,handlers=[handler],format='%(asctime)s %(levelname)s %(message)s')
        manifest = verify_package(package,full=True)
        check_data_root(data)
        if (data/'data/protocol_demo.db').exists():
            validate_data(package,data,manifest)
        if args.launcher_pid:
            from product.windows import watch_parent
            watch_parent(args.launcher_pid)
        supervisor = Supervisor(package,data,manifest,args.no_browser,args.launcher_pid)
        supervisor.run()
        report({'status':'stopped'})
        return 0
    except Exception:
        logging.exception('Desktop startup failed')
        raise
    finally:
        lock.close()


def entry():
    try:
        return main()
    except Exception as exc:
        # Readable failure even when Explorer starts a windowless executable.
        if '--report' in sys.argv:
            index = sys.argv.index('--report')
            if index+1 < len(sys.argv):
                write_json(sys.argv[index+1],{'status':'failed','error':str(exc)})
        if '--no-browser' not in sys.argv:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,str(exc),'PipelinePer 启动失败',0x10)
        return 1
