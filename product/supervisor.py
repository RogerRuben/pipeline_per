"""Own and supervise three independently controllable local services."""
from collections import deque
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import queue
import secrets
import subprocess
import threading
import time
from urllib.parse import urlparse
import webbrowser

from product.common import read_json,write_json
from product.desktop_config import load_config,ROLES
from product.recovery import backup_data
from product.windows import WorkerJob


def fetch(url,payload=None,token=None,timeout=3):
    from urllib.request import Request,build_opener,ProxyHandler
    headers={'Content-Type':'application/json'}
    if token:headers['X-Product-Token']=token
    data=None if payload is None else json.dumps(payload).encode('utf-8')
    with build_opener(ProxyHandler({})).open(Request(url,data=data,headers=headers),timeout=timeout) as response:
        return json.load(response)


class Supervisor:
    def __init__(self,package,data,manifest,no_browser=False,launcher_pid=None):
        self.package,self.data,self.manifest=Path(package),Path(data),manifest
        self.config=load_config(package,data);self.ports=dict(self.config['ports'])
        self.no_browser=no_browser or not self.config['open_browser']
        self.launcher_pid=launcher_pid or os.getpid()
        self.instance_id=secrets.token_hex(16);self.token=secrets.token_urlsafe(32)
        self.stopping=threading.Event();self.restarting=threading.Event();self.lock=threading.RLock()
        self.workers={};self.logs={};self.paused=set();self.blocked=set();self.errors={}
        self.generation=secrets.token_hex(8);self.phase='starting';self.error=None;self.restart_count=0
        self.failures={role:deque() for role in ROLES};self.health_failures={role:0 for role in ROLES}
        self.actions=queue.Queue(maxsize=32);self.operations=deque(maxlen=30)
        self.job=WorkerJob()
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
        try:self.server=ThreadingHTTPServer(('127.0.0.1',self.ports['control']),self.handler())
        except OSError as exc:
            self.job.close()
            raise RuntimeError('管理端口 %d 无法使用，请检查 config/desktop.json：%s'%(self.ports['control'],exc))
        self.server.daemon_threads=True;self.ports['control']=self.server.server_address[1]
        self.url=self.service_url('control')
        write_json(self.data/'runtime/control.json',{'url':self.url,'token':self.token,
                   'instance_id':self.instance_id,'pid':self.launcher_pid,'supervisor_pid':os.getpid()})
        self.record_status()

    def service_url(self,role):
        return 'http://127.0.0.1:%d'%self.ports[role] if self.ports[role] else None

    def alive(self,role):
        item=self.workers.get(role)
        return bool(item and item['process'].poll() is None and item.get('url'))

    def status(self):
        with self.lock:
            services={}
            for role in ROLES:
                item=self.workers.get(role);process=item['process'] if item else None
                services[role]={'pid':process.pid if process and process.poll() is None else None,
                   'url':self.service_url(role),'alive':self.alive(role),'paused':role in self.paused,
                   'state':'running' if self.alive(role) else 'stopped' if role in self.paused else 'failed' if role in self.blocked else 'starting',
                   'error':self.errors.get(role)}
            web=self.service_url('web')
            return {'status':self.phase,'error':self.error,'instance_id':self.instance_id,
                    'pid':self.launcher_pid,'supervisor_pid':os.getpid(),'version':self.manifest['version'],
                    'package_dir':str(self.package),'data_dir':str(self.data),
                    'classification':self.manifest['release']['classification'],'win7_validated':False,
                    'generation':self.generation,'restart_count':self.restart_count,
                    'portal_url':web+'/portal' if web else None,'control_url':self.service_url('control'),
                    'ports':dict(self.ports),'services':services,'operations':[dict(item) for item in self.operations],
                    'config_path':str(self.data/'config/desktop.json')}

    def record_status(self):
        status=self.status();write_json(self.data/'runtime/status.json',status)
        write_json(self.data/'runtime/addresses.json',{'portal':status['portal_url'],'management':status['control_url'],
                   'services':{role:item['url'] for role,item in status['services'].items()}})
        text='PipelinePer\nPortal: %s\nManagement: %s\nConfig: %s\n'%(status['portal_url'],self.service_url('control'),status['config_path'])
        (self.data/'runtime/access.txt').write_text(text,encoding='utf-8')

    def handler(self):
        supervisor=self
        class ControlHandler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send(self,status,payload,content_type='application/json; charset=utf-8'):
                raw=payload if isinstance(payload,bytes) else payload.encode('utf-8') if isinstance(payload,str) else json.dumps(payload,ensure_ascii=False).encode('utf-8')
                try:
                    self.send_response(status);self.send_header('Content-Type',content_type)
                    self.send_header('Content-Length',str(len(raw)));self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; frame-ancestors 'none'")
                    self.end_headers();self.wfile.write(raw)
                except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):self.close_connection=True
            def host_valid(self):return self.headers.get('Host')==urlparse(supervisor.url).netloc
            def do_GET(self):
                if not self.host_valid():return self.send(403,{'error':'invalid_host'})
                if self.path=='/api/status':return self.send(200,supervisor.status())
                if self.path=='/launcher.svg':return self.send(200,(supervisor.package/'payload/app/static/launcher.svg').read_bytes(),'image/svg+xml')
                if self.path=='/':
                    page=(supervisor.package/'payload/product/control.html').read_text(encoding='utf-8')
                    return self.send(200,page.replace('__TOKEN_JSON__',json.dumps(supervisor.token)),'text/html; charset=utf-8')
                self.send(404,{'error':'not_found'})
            def do_POST(self):
                self.close_connection=True
                if (not self.host_valid() or self.headers.get('Origin',supervisor.url)!=supervisor.url
                    or self.headers.get('Sec-Fetch-Site')=='cross-site'
                    or not secrets.compare_digest(self.headers.get('X-Product-Token',''),supervisor.token)):
                    return self.send(403,{'error':'forbidden'})
                try:
                    length=int(self.headers.get('Content-Length','0'))
                    if not 0<=length<=1024:return self.send(413,{'error':'body_too_large'})
                    self.connection.settimeout(2);self.rfile.read(length)
                except (ValueError,OSError):return self.send(400,{'error':'invalid_body'})
                if self.path=='/api/stop':
                    self.send(200,{'status':'stopping'});supervisor.stopping.set();return
                if self.path=='/api/backup':
                    try:return self.send(200,{'backup':str(backup_data(supervisor.data))})
                    except Exception as exc:
                        logging.exception('Backup failed');return self.send(500,{'error':str(exc)})
                if self.path=='/api/restart':role,action='all','restart'
                else:
                    parts=self.path.split('/')
                    if len(parts)!=5 or parts[:3]!=['','api','services'] or parts[3] not in ROLES or parts[4] not in ('start','stop','restart'):
                        return self.send(404,{'error':'not_found'})
                    role,action=parts[3:]
                operation={'id':secrets.token_hex(8),'role':role,'action':action,'status':'queued'}
                try:supervisor.actions.put_nowait(operation)
                except queue.Full:return self.send(429,{'error':'operation_queue_full'})
                with supervisor.lock:supervisor.operations.append(operation)
                self.send(202,{'status':'queued','operation_id':operation['id']})
        return ControlHandler

    def start_worker(self,role):
        if role=='web' and not all(self.alive(dep) for dep in ('price','effectiveness')):
            raise RuntimeError('请先启动价格和效能服务，再启动网页服务。')
        ready=self.data/('runtime/'+secrets.token_hex(12)+'-'+role+'.json')
        logfile=self.data/('logs/'+role+'.log');logfile.parent.mkdir(parents=True,exist_ok=True)
        if logfile.exists() and logfile.stat().st_size>5*1024*1024:
            os.replace(str(logfile),str(logfile.with_suffix('.previous.log')))
        stream=logfile.open('ab',buffering=0);self.logs[role]=stream
        args=[str(self.package/'runtime/python38/pythonw.exe'),'-I','-B','-X','utf8',
              str(self.package/'payload/product/worker.py'),'--package',str(self.package),
              '--data-dir',str(self.data),'--role',role,'--ready',str(ready),'--generation',self.generation,
              '--parent',str(os.getpid()),'--port',str(self.ports[role])]
        if role=='web':args.extend(['--price-url',self.service_url('price'),'--effectiveness-url',self.service_url('effectiveness')])
        env=dict(os.environ)
        for key in list(env):
            if key.startswith(('PYTHON','IPDEMO_','_PYI','_MEIPASS')):env.pop(key,None)
        env.update(OMP_NUM_THREADS='1',OPENBLAS_NUM_THREADS='1',MKL_NUM_THREADS='1',NUMEXPR_NUM_THREADS='1')
        process=subprocess.Popen(args,cwd=str(self.data),env=env,stdin=subprocess.DEVNULL,
                                 stdout=stream,stderr=stream,creationflags=subprocess.CREATE_NO_WINDOW)
        with self.lock:self.workers[role]={'process':process,'url':None}
        try:
            self.job.assign(process);deadline=time.monotonic()+60
            while time.monotonic()<deadline and not self.stopping.is_set():
                if process.poll() is not None:raise RuntimeError('%s 服务启动失败；端口 %d；日志 %s'%(role,self.ports[role],logfile))
                if ready.exists():
                    value=read_json(ready)
                    if value['pid']==process.pid and value['generation']==self.generation:
                        try:
                            health=fetch(value['url']+('/api/health' if role=='web' else '/health'),timeout=1)
                            if health.get('status')=='ok':
                                with self.lock:
                                    self.workers[role]['url']=value['url'];self.ports[role]=urlparse(value['url']).port
                                    self.errors.pop(role,None);self.health_failures[role]=0
                                return value['url']
                        except (OSError,ValueError):pass
                self.stopping.wait(.1)
            raise RuntimeError(role+' 启动超时或已取消；请查看 '+str(logfile))
        finally:
            if ready.exists():ready.unlink()

    def stop_worker(self,role):
        with self.lock:item=self.workers.get(role)
        if item:
            process=item['process']
            if process.poll() is None:process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)
            with self.lock:self.workers.pop(role,None)
        stream=self.logs.pop(role,None)
        if stream:stream.close()

    def stop_workers(self):
        for role in reversed(ROLES):self.stop_worker(role)

    def settle(self):
        self.phase='ready' if all(self.alive(role) for role in ROLES) else 'degraded'
        self.record_status()

    def start_workers(self):
        self.phase='starting';self.error=None;self.generation=secrets.token_hex(8)
        for role in ROLES:self.start_worker(role)
        self.settle();logging.info('All services ready generation=%s',self.generation)

    def execute(self,operation):
        role,action=operation['role'],operation['action'];operation['status']='running'
        self.phase='starting';self.error=None;self.generation=secrets.token_hex(8)
        try:
            if role=='all':
                self.paused.clear();self.blocked.clear()
                for item in self.failures.values():item.clear()
                self.stop_workers();self.restart_count+=1;self.start_workers()
            else:
                self.blocked.discard(role);self.failures[role].clear()
                if action=='stop':self.paused.add(role)
                else:self.paused.discard(role)
                if action in ('stop','restart'):self.stop_worker(role)
                if action in ('start','restart') and not self.alive(role):self.start_worker(role)
                self.restart_count+=int(action=='restart');self.settle()
            operation['status']='succeeded'
        except Exception as exc:
            logging.exception('Service operation failed')
            if role in ROLES:
                self.errors[role]=str(exc);self.blocked.add(role);self.stop_worker(role)
            self.error=str(exc);operation.update(status='failed',error=str(exc));self.settle()
        self.record_status()

    def run(self):
        thread=threading.Thread(target=self.server.serve_forever,daemon=True);thread.start()
        try:
            installed=self.data/'installed.json';old=read_json(installed) if installed.exists() else None
            if old and old.get('data_schema')!=self.manifest['data_schema']:
                raise ValueError('Data schema changed; explicit migration required')
            if old and old.get('version')!=self.manifest['version']:logging.info('Pre-update backup: %s',backup_data(self.data))
            self.start_workers()
            write_json(installed,{'version':self.manifest['version'],'data_schema':self.manifest['data_schema'],
                                 'release':self.manifest['release']['product_code']})
            if not self.no_browser:webbrowser.open(self.status()['portal_url'])
            next_probe=time.monotonic()+5
            while not self.stopping.wait(.25):
                if self.restarting.is_set():
                    self.restarting.clear();self.execute({'role':'all','action':'restart','status':'queued'})
                try:operation=self.actions.get_nowait()
                except queue.Empty:operation=None
                if operation:self.execute(operation)
                now=time.monotonic();probe=now>=next_probe
                if probe:next_probe=now+5
                for role in ROLES:
                    if role in self.paused or role in self.blocked:continue
                    bad=not self.alive(role)
                    if probe and not bad:
                        try:
                            if fetch(self.service_url(role)+('/api/health' if role=='web' else '/health'),timeout=1).get('status')!='ok':raise ValueError('Unhealthy')
                            self.health_failures[role]=0
                        except (OSError,ValueError):self.health_failures[role]+=1
                        bad=self.health_failures[role]>=3
                    if not bad:continue
                    failures=self.failures[role];failures.append(now)
                    while failures and now-failures[0]>60:failures.popleft()
                    self.stop_worker(role);self.generation=secrets.token_hex(8)
                    if len(failures)>3:
                        self.errors[role]='Repeated failures; inspect logs and start manually';self.blocked.add(role);self.settle();continue
                    self.phase='starting';self.restart_count+=1
                    try:self.start_worker(role)
                    except Exception as exc:
                        logging.exception('Automatic service restart failed');self.errors[role]=str(exc);self.blocked.add(role);self.stop_worker(role)
                    self.settle()
                if probe:self.record_status()
        finally:
            self.phase='stopping';self.stop_workers();self.job.close();self.phase='stopped';self.record_status()
            self.server.shutdown();self.server.server_close();thread.join(timeout=5)
            try:(self.data/'runtime/control.json').unlink()
            except FileNotFoundError:pass
