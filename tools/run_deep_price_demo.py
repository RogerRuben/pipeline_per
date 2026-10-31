"""Isolated local virtual-data demo; not a production model or EXE launcher."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import subprocess
import threading
import time
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.server import Application, Handler
from services.price_service.app import PriceService
from services.effectiveness_service.app import EffectivenessService, SnapshotBackend
from services.common.http_service import make_handler


def main():
    parser=argparse.ArgumentParser(description='仅本机虚拟数据验收环境；不修改生产库、不训练')
    parser.add_argument('--port',type=int,default=18891)
    parser.add_argument('--data-dir',default=str(ROOT/'outputs/deep_price_demo'))
    parser.add_argument('--test-predict-delay',type=float,default=0,help='仅虚拟浏览器竞态验收：单预测响应延时0至10秒')
    args=parser.parse_args()
    if not 0<=args.test_predict_delay<=10:
        parser.error('测试延时必须在0至10秒')
    data=Path(args.data_dir).resolve()
    if data==ROOT or ROOT not in data.parents:
        raise SystemExit('演示数据目录必须为工程内的独立子目录')
    data.mkdir(parents=True,exist_ok=True)
    for name in ('models','data_master'):
        if not (data/name).exists():
            shutil.copytree(str(ROOT/name),str(data/name))
    (data/'app').mkdir(exist_ok=True)
    shutil.copytree(str(ROOT/'app/static'),str(data/'app/static'),dirs_exist_ok=True)
    (data/'config').mkdir(exist_ok=True)
    servers=[]
    try:
        class DemoPriceService(PriceService):
            def handle_post(self,path,payload):
                if path=='/api/v1/predict' and args.test_predict_delay:
                    time.sleep(args.test_predict_delay)
                return super().handle_post(path,payload)
        for service in (DemoPriceService(None,data/'models/price_bundle.json'),
                        EffectivenessService(SnapshotBackend(data/'models/effectiveness_bundle.json'))):
            server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(service))
            server.daemon_threads=True
            threading.Thread(target=server.serve_forever,daemon=True).start()
            servers.append(server)
        config={'execution_mode':'services','local_fallback':False,
                'price_service_url':'http://127.0.0.1:%d'%servers[0].server_address[1],
                'effectiveness_service_url':'http://127.0.0.1:%d'%servers[1].server_address[1],'timeout_seconds':15}
        (data/'config/model_services.json').write_text(json.dumps(config),encoding='utf-8')
        # This is a separate, loopback-only virtual environment. No credentials
        # or auth settings in the normal installation are touched.
        for key in ('IPDEMO_MODEL_EXECUTION_MODE','IPDEMO_PRICE_SERVICE_URL','IPDEMO_EFFECT_SERVICE_URL'):
            os.environ.pop(key,None)
        os.environ['IPDEMO_AUTH_ENABLED']='0'
        os.environ['IPDEMO_DEMO_READ_ONLY']='0'
        app=Application(data)
        if not any(r['status']=='active' for r in app.store.list_product_releases()):
            release=app.product_releases.clone_current()
            app.product_releases.activate(release['release_id'])
        app.portal_config['title']='工业产品决策平台 · 虚拟数据验收'
        class DemoHandler(Handler):pass
        DemoHandler.app=app
        portal=ThreadingHTTPServer(('127.0.0.1',args.port),DemoHandler)
        portal.daemon_threads=True
        info={'url':'http://127.0.0.1:%d/portal'%portal.server_address[1],
              'deep_url':'http://127.0.0.1:%d/price/deep'%portal.server_address[1],
              'virtual_fixture':True,'price_url':config['price_service_url'],'effectiveness_url':config['effectiveness_service_url'],
              'pid':os.getpid(),'data_dir':str(data),
              'test_predict_delay_seconds':args.test_predict_delay,
              'source_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=str(ROOT),text=True).strip()}
        (data/'demo_running.json').write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(info,ensure_ascii=True),flush=True)
        try:portal.serve_forever()
        finally:portal.server_close()
    except KeyboardInterrupt:pass
    finally:
        for server in servers:server.shutdown();server.server_close()


if __name__=='__main__':main()
