"""Separate frozen expert process; original dependencies do not affect price."""
import argparse,json,sys,os,traceback
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from product.common import read_json,write_json,verify_package
from product.windows import watch_parent
from services.effectiveness_service.expert_packages import load_backend
from services.effectiveness_service.app import EffectivenessService
def main():
 if '--asset-check' in sys.argv:
  index=sys.argv.index('--asset-check');role,path,metadata,report=sys.argv[index+1:index+5]
  try:
   service=EffectivenessService(load_backend(path));schema=service.schema();ref=schema.get('reference_parameters') or service.example_request()['parameters']
   probe=service.handle_post('/api/v1/evaluate',{'parameters':ref});write_json(report,{'status':'pass','schema':schema,'health':service.health(),'probe':probe});return 0
  except Exception as exc:write_json(report,{'status':'fail','message':str(exc)});return 1
 p=argparse.ArgumentParser();p.add_argument('--worker',action='store_true');p.add_argument('--role')
 for flag in ['package','data-dir','ready','generation']:p.add_argument('--'+flag,required=True)
 p.add_argument('--parent',type=int,required=True);p.add_argument('--port',type=int,default=0);p.add_argument('--reserved-ports',default='');a=p.parse_args();watch_parent(a.parent)
 data=Path(a.data_dir);package=Path(a.package)
 if getattr(sys,'frozen',False):sys.stdout=sys.stderr=(data/'logs/effectiveness.log').open('a',encoding='utf-8',buffering=1)
 manifest=verify_package(package)
 from product.model_library import active_paths
 selected=active_paths(package,data,manifest['release'],'effectiveness');service=EffectivenessService(load_backend(selected['effectiveness']))
 from services.common.http_service import ExclusiveThreadingHTTPServer,make_handler
 from product.ports import bind_server
 server=bind_server(ExclusiveThreadingHTTPServer,a.port,make_handler(service),[int(x) for x in a.reserved_ports.split(',') if x]);server.daemon_threads=True
 write_json(a.ready,{'pid':os.getpid(),'role':'effectiveness','generation':a.generation,'url':'http://127.0.0.1:%d'%server.server_address[1]})
 try:server.serve_forever()
 finally:server.server_close()
if __name__=='__main__':
 try:result=main()
 except Exception:
  if sys.stderr:traceback.print_exc()
  result=1
 raise SystemExit(result)
