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


class Supervisor:
    def __init__(self,package,data,manifest,no_browser=False):
        self.package,self.data,self.manifest = Path(package),Path(data),manifest
        self.no_browser = no_browser
        self.instance_id = secrets.token_hex(16)
        self.token = secrets.token_urlsafe(32)
        self.stopping = threading.Event()
        self.restarting = threading.Event()
        self.lock = threading.RLock()
        self.workers = {}
        self.logs = {}
        self.generation = ''
        self.phase = 'starting'
        self.error = None
        self.restart_count = 0
        self.failures = deque()
        self.job = WorkerJob()
        # PyInstaller sets a DLL search directory for its own interpreter. Child
        # model interpreters must resolve their adjacent pinned DLLs instead.
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),self.handler())
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:%d'%self.server.server_address[1]
        write_json(data/'runtime/control.json',{'url':self.url,'token':self.token,
                   'instance_id':self.instance_id,'pid':os.getpid()})

    def status(self):
        with self.lock:
            return {'status':self.phase,'error':self.error,'instance_id':self.instance_id,
                    'pid':os.getpid(),'version':self.manifest['version'],'package_dir':str(self.package),
                    'data_dir':str(self.data),'classification':self.manifest['release']['classification'],
                    'win7_validated':False,'generation':self.generation,'restart_count':self.restart_count,
                    'portal_url':self.workers.get('web',{}).get('url','')+'/portal' if 'web' in self.workers else None,
                    'services':{key:{'pid':item['process'].pid,'url':item.get('url'),
                                    'alive':item['process'].poll() is None} for key,item in self.workers.items()}}

    def handler(self):
        supervisor = self
        class ControlHandler(BaseHTTPRequestHandler):
            def log_message(self,*args):
                pass

            def send(self,status,payload,content_type='application/json; charset=utf-8'):
                raw = payload.encode('utf-8') if isinstance(payload,str) else json.dumps(payload,ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(status)
                    self.send_header('Content-Type',content_type)
                    self.send_header('Content-Length',str(len(raw)))
                    self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):
                    self.close_connection = True

            def host_valid(self):
                return self.headers.get('Host') == urlparse(supervisor.url).netloc

            def do_GET(self):
                if not self.host_valid():
                    return self.send(403,{'error':'invalid_host'})
                if self.path == '/api/status':
                    return self.send(200,supervisor.status())
                if self.path == '/':
                    page = (supervisor.package/'payload/product/control.html').read_text(encoding='utf-8')
                    page = page.replace('__TOKEN_JSON__',json.dumps(supervisor.token))
                    return self.send(200,page,'text/html; charset=utf-8')
                self.send(404,{'error':'not_found'})

            def do_POST(self):
                self.close_connection = True
                if (not self.host_valid() or self.headers.get('Origin',supervisor.url) != supervisor.url
                    or self.headers.get('Sec-Fetch-Site') == 'cross-site'
                    or not secrets.compare_digest(self.headers.get('X-Product-Token',''),supervisor.token)):
                    return self.send(403,{'error':'forbidden'})
                if self.path == '/api/stop':
                    self.send(200,{'status':'stopping'})
                    supervisor.stopping.set()
                elif self.path == '/api/restart':
                    self.send(202,{'status':'restarting'})
                    supervisor.restarting.set()
                elif self.path == '/api/backup':
                    try:
                        self.send(200,{'backup':str(backup_data(supervisor.data))})
                    except Exception as exc:
                        logging.exception('Backup failed')
                        self.send(500,{'error':str(exc)})
                else:
                    self.send(404,{'error':'not_found'})
        return ControlHandler

    def start_worker(self,role,**urls):
        ready = self.data/('runtime/'+self.generation+'-'+role+'.json')
        logfile = self.data/('logs/'+role+'.log')
        # Bound disk growth on restart; never rotate an open child log.
        if logfile.exists() and logfile.stat().st_size > 5*1024*1024:
            os.replace(str(logfile),str(logfile.with_suffix('.previous.log')))
        stream = logfile.open('ab',buffering=0)
        self.logs[role] = stream
        args = [str(self.package/'runtime/python38/pythonw.exe'),'-I','-B','-X','utf8',
                str(self.package/'payload/product/worker.py'),'--package',str(self.package),
                '--data-dir',str(self.data),'--role',role,'--ready',str(ready),
                '--generation',self.generation,'--parent',str(os.getpid())]
        for key,value in urls.items():
            args.extend(['--'+key.replace('_','-'),value])
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(('PYTHON','IPDEMO_','_PYI','_MEIPASS')):
                env.pop(key,None)
        env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',
                   NUMEXPR_NUM_THREADS='1',PYTHONDONTWRITEBYTECODE='1')
        process = subprocess.Popen(args,cwd=str(self.data),env=env,stdin=subprocess.DEVNULL,
                                   stdout=stream,stderr=stream,creationflags=subprocess.CREATE_NO_WINDOW)
        with self.lock:
            self.workers[role] = {'process':process,'url':None}
        try:
            self.job.assign(process)
            deadline = time.monotonic()+60
            while time.monotonic() < deadline and not self.stopping.is_set():
                if process.poll() is not None:
                    raise RuntimeError(role+' exited; see '+str(logfile))
                if ready.is_file():
                    value = read_json(ready)
                    if value['pid'] == process.pid and value['generation'] == self.generation:
                        endpoint = '/api/health' if role == 'web' else '/health'
                        try:
                            health = fetch(value['url']+endpoint,timeout=1)
                            if health.get('status') == 'ok':
                                with self.lock:
                                    self.workers[role]['url'] = value['url']
                                return value['url']
                        except (OSError,ValueError):
                            pass
                self.stopping.wait(.1)
            raise RuntimeError(role+' startup timed out or cancelled; see '+str(logfile))
        finally:
            if ready.exists():
                ready.unlink()

    def stop_workers(self):
        with self.lock:
            items = list(self.workers.values())
        # Handles from Popen, never stale PIDs or image-name-wide taskkill.
        for item in reversed(items):
            process = item['process']
            if process.poll() is None:
                process.terminate()
        for item in items:
            try:
                item['process'].wait(timeout=10)
            except subprocess.TimeoutExpired:
                item['process'].kill()
                item['process'].wait(timeout=5)
        for stream in self.logs.values():
            stream.close()
        with self.lock:
            self.workers.clear()
        self.logs.clear()

    def start_workers(self):
        with self.lock:
            self.phase = 'starting'
            self.error = None
            self.generation = secrets.token_hex(8)
        price = self.start_worker('price')
        effect = self.start_worker('effectiveness')
        self.start_worker('web',price_url=price,effectiveness_url=effect)
        with self.lock:
            self.phase = 'ready'
        write_json(self.data/'runtime/status.json',self.status())
        logging.info('Services ready generation=%s',self.generation)

    def run(self):
        thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        thread.start()
        try:
            installed = self.data/'installed.json'
            old = read_json(installed) if installed.is_file() else None
            if old and old.get('data_schema') != self.manifest['data_schema']:
                raise ValueError('Data schema changed; migration must be explicitly validated')
            if old and old.get('version') != self.manifest['version']:
                backup = backup_data(self.data)
                logging.info('Pre-update backup: %s',backup)
            self.start_workers()
            write_json(installed,{'version':self.manifest['version'],'data_schema':self.manifest['data_schema'],
                                 'release':self.manifest['release']['product_code']})
            if not self.no_browser:
                webbrowser.open(self.status()['portal_url'])
            health_failures = {}
            next_probe = time.monotonic()+5
            while not self.stopping.wait(1):
                requested = self.restarting.is_set()
                self.restarting.clear()
                with self.lock:
                    dead = [role for role,item in self.workers.items() if item['process'].poll() is not None]
                    probes = [(role,item['url']) for role,item in self.workers.items() if item.get('url')]
                if not dead and time.monotonic() >= next_probe:
                    next_probe = time.monotonic()+5
                    for role,url in probes:
                        try:
                            endpoint = '/api/health' if role=='web' else '/health'
                            if fetch(url+endpoint,timeout=1).get('status') != 'ok':
                                raise ValueError('Unhealthy service')
                            health_failures[role] = 0
                        except (OSError,ValueError):
                            health_failures[role] = health_failures.get(role,0)+1
                            if health_failures[role] >= 3:
                                dead.append(role+' (health check failed)')
                if not requested and not dead:
                    continue
                if dead and not requested:
                    now = time.monotonic()
                    self.failures.append(now)
                    while self.failures and now-self.failures[0] > 60:
                        self.failures.popleft()
                    if len(self.failures) > 3:
                        self.phase,self.error = 'failed','Repeated worker exits; inspect logs and restart manually'
                        self.stop_workers()
                        write_json(self.data/'runtime/status.json',self.status())
                        continue
                logging.warning('Restarting services; requested=%s dead=%s',requested,dead)
                if requested:
                    self.failures.clear()
                health_failures.clear()
                self.stop_workers()
                self.restart_count += 1
                try:
                    self.start_workers()
                except Exception as exc:
                    logging.exception('Service restart failed')
                    self.stop_workers()
                    self.phase,self.error = 'failed',str(exc)
                    write_json(self.data/'runtime/status.json',self.status())
        finally:
            self.phase = 'stopping'
            self.stop_workers()
            self.job.close()
            self.phase = 'stopped'
            write_json(self.data/'runtime/status.json',self.status())
            self.server.shutdown()
            self.server.server_close()
            thread.join(timeout=5)
            try:
                (self.data/'runtime/control.json').unlink()
            except FileNotFoundError:
                pass


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--data-dir')
    parser.add_argument('--no-browser',action='store_true')
    parser.add_argument('--report',help='Write command result as UTF-8 JSON (windowless EXE)')
    group = parser.add_mutually_exclusive_group()
    for command in ('stop','status','restart','backup','verify'):
        group.add_argument('--'+command,action='store_true')
    group.add_argument('--restore',metavar='BACKUP_ZIP',help='Offline restore into a new --data-dir; never overwrite userdata')
    args = parser.parse_args(argv)
    package = Path(sys.executable).resolve().parent if getattr(sys,'frozen',False) else Path(__file__).resolve().parents[1]
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
    if any((args.stop,args.status,args.restart,args.backup)):
        if not existing:
            report({'status':'stopped'})
            return 0 if args.stop or args.status else 1
        control,status = existing
        command = 'stop' if args.stop else 'restart' if args.restart else 'backup' if args.backup else None
        report(fetch(control['url']+'/api/'+command,{},control['token']) if command else status)
        return 0
    data.mkdir(parents=True,exist_ok=True)
    try:
        lock = InstanceLock(data)
    except RuntimeError:
        # A double click during cold start waits for the same instance.
        deadline = time.monotonic()+75
        while time.monotonic() < deadline:
            existing = running(data)
            if existing and existing[1]['status'] in ('ready','failed'):
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
        supervisor = Supervisor(package,data,manifest,args.no_browser)
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
