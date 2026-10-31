"""Serve a verified V3 artifact in a previously isolated acceptance workspace."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tools')]
from verify_v3_release import preflight,serving
from bind_price_evaluation_metadata import bind
from app.server import Application,Handler
from services.price_service.app import PriceService
from services.effectiveness_service.app import EffectivenessService,SnapshotBackend


def main():
    p=argparse.ArgumentParser();p.add_argument('--build-dir',required=True);p.add_argument('--workspace',required=True);p.add_argument('--port',type=int,default=18893);p.add_argument('--classification',choices=['synthetic','unconfirmed','formal'],default='unconfirmed');a=p.parse_args()
    manifest,paths,source,env=preflight(a.build_dir,a.classification)
    workspace=Path(a.workspace).resolve()
    if ROOT not in workspace.parents or workspace.parts[len(ROOT.parts)]!='outputs' or not (workspace/'data/protocol_demo.db').is_file():
        raise ValueError('须使用verify_v3_release创建的工程outputs内隔离workspace')
    classification='synthetic' if manifest.get('virtual_data') else a.classification
    metadata=bind(paths['price_model'],paths['price_model'].parent/'training_report.json',workspace/('evaluation_%d.json'%time.time_ns()),classification)
    with serving(PriceService(paths['price_model'],None,evaluation_metadata=metadata)) as price_url,serving(EffectivenessService(SnapshotBackend(paths['effectiveness_model']))) as effect_url:
        (workspace/'config/model_services.json').write_text(json.dumps({'execution_mode':'services','price_service_url':price_url,'effectiveness_service_url':effect_url,'local_fallback':False}),encoding='utf-8')
        os.environ.update(IPDEMO_MODEL_EXECUTION_MODE='services',IPDEMO_PRICE_SERVICE_URL=price_url,IPDEMO_EFFECT_SERVICE_URL=effect_url,IPDEMO_AUTH_ENABLED='0',IPDEMO_DEMO_READ_ONLY='0')
        app=Application(workspace);app.static_dir=ROOT/'app/static'
        class AcceptanceHandler(Handler):pass
        AcceptanceHandler.app=app
        server=ThreadingHTTPServer(('127.0.0.1',a.port),AcceptanceHandler);server.daemon_threads=True
        print(json.dumps({'url':'http://127.0.0.1:%d/portal'%a.port,'classification':classification,'native_model':True,'pid':os.getpid()},ensure_ascii=True),flush=True)
        try:server.serve_forever()
        except KeyboardInterrupt:pass
        finally:server.server_close()


if __name__=='__main__':main()
