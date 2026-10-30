"""Read existing V3 releases; validate only in a new, isolated workspace.

No training, production activation, UI changes or implicit model fallback.
All reports stay in the selected local output directory and may contain
business metadata. Do not publish them without a separate data review.
"""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import threading
import time
from unittest.mock import patch
from http.server import ThreadingHTTPServer

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def preflight(build,classification):
    build=Path(build).resolve()
    manifest=json.loads((build/'prototype_manifest.json').read_text(encoding='utf-8-sig'))
    if manifest.get('price_engine')!='optuna_v3' or manifest.get('status')!='ready':
        raise ValueError('必须为已完成的显式optuna_v3构建，不能使用默认V7或未完成目录')
    if classification=='formal' and manifest.get('virtual_data') is not False:
        raise ValueError('正式数据验收要求构建清单明确virtual_data=false；不能将虚拟构建改标正式')
    paths={}
    for key in ('price_model','effectiveness_model','business_package'):
        p=(build/manifest[key]).resolve()
        if build not in p.parents or sha(p)!=manifest['artifact_sha256'][key]:
            raise ValueError('构建产物路径/哈希不匹配：'+key)
        paths[key]=p
    source=(build/'input'/manifest['source_filename']).resolve()
    if build not in source.parents or sha(source)!=manifest['source_sha256']:
        raise ValueError('products源文件与构建清单SHA不一致')
    from services.price_service.native_bundle import environment_versions
    recorded=json.loads(Path(str(paths['price_model'])+'.manifest.json').read_text(encoding='utf-8-sig'))
    if recorded.get('bundle_sha256',recorded.get('sha256'))!=sha(paths['price_model']):
        raise ValueError('原生模型清单SHA与模型不一致')
    environment=environment_versions()
    for name,version in (recorded.get('training_environment') or {}).items():
        if name in ('numpy','scikit_learn','joblib','xgboost') and version is not None and environment.get(name)!=version:
            raise ValueError('运行依赖与所选训练模型不一致：'+name)
    return manifest,paths,source,environment


@contextmanager
def serving(app):
    from services.common.http_service import make_handler
    handler=make_handler(app)
    handler.log_message=lambda *args:None
    server=ThreadingHTTPServer(('127.0.0.1',0),handler)
    server.daemon_threads=True
    worker=threading.Thread(target=server.serve_forever,daemon=True)
    worker.start()
    try:yield 'http://127.0.0.1:%d'%server.server_address[1]
    finally:
        server.shutdown();server.server_close();worker.join(timeout=5)


def verify(build,output,classification='unconfirmed',repeats=3,replacement_build=None):
    manifest,paths,source,environment=preflight(build,classification)
    output=Path(output).resolve()
    if output==ROOT or ROOT not in output.parents or output.parts[len(ROOT.parts)]!='outputs':
        raise ValueError('验收输出须为本项目outputs下的新目录')
    if output.exists():raise ValueError('验收目录已存在，不能覆盖')
    output.mkdir(parents=True)
    from app.server import Application
    from app.store import Store
    from app.product_releases import ProductReleaseService
    from app.model_service_client import ModelServiceGateway,DegradedServiceRuntime
    from app.deep_price import DeepPriceAnalysis
    from app.deep_price.features import AnalysisFeatures,IDENTITY
    from services.price_service.app import PriceService
    from services.effectiveness_service.app import EffectivenessService,SnapshotBackend
    runtime=DegradedServiceRuntime(None,manifest['product_code'],manifest['product_name'])
    store=Store(output/'workspace/data/protocol_demo.db',output/'unused.csv',runtime)
    releases=ProductReleaseService(store,runtime)
    release=releases.import_package(paths['business_package'].read_bytes())
    releases.activate(release['release_id'])
    workspace=output/'workspace'
    (workspace/'config').mkdir(exist_ok=True)
    report={'classification':classification,'formal_acceptance':False,'source_sha':subprocess.check_output(['git','rev-parse','HEAD'],cwd=str(ROOT),encoding='utf-8').strip(),
            'product_source_sha256':sha(source),'model_sha256':sha(paths['price_model']),
            'environment':environment,'host':platform.platform(),'win7_validated':False,
            'virtual_data_declared':manifest.get('virtual_data'),'checks':{},'timings':{}}
    def save():
        (output/'acceptance.local.json').write_text(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')
    before={k:sha(p) for k,p in paths.items()}
    def timed(label,fn):
        started=time.monotonic()
        try:return fn()
        except Exception as exc:
            report.update(status='fail',failed_stage=label,error=str(exc))
            raise
        finally:
            report['timings'].setdefault(label,[]).append(round(time.monotonic()-started,6));save()
    from tools.bind_price_evaluation_metadata import bind
    metadata=bind(paths['price_model'],paths['price_model'].parent/'training_report.json',output/'evaluation_metadata.local.json','synthetic' if manifest.get('virtual_data') else classification)
    price=timed('model_load',lambda:PriceService(paths['price_model'],None,evaluation_metadata=metadata))
    report['model']={k:price.health().get(k) for k in ('model_version','model_count','model_names','backend','exact_mode')}
    report['runtime_workspace_contains_training_products']=False
    with serving(price) as price_url,serving(EffectivenessService(SnapshotBackend(paths['effectiveness_model']))) as effect_url:
        config={'execution_mode':'services','price_service_url':price_url,'effectiveness_service_url':effect_url,'local_fallback':False}
        (workspace/'config/model_services.json').write_text(json.dumps(config),encoding='utf-8')
        saved={k:os.environ.get(k) for k in ('IPDEMO_MODEL_EXECUTION_MODE','IPDEMO_PRICE_SERVICE_URL','IPDEMO_EFFECT_SERVICE_URL','IPDEMO_AUTH_ENABLED','IPDEMO_DEMO_READ_ONLY')}
        try:
            os.environ.update(IPDEMO_MODEL_EXECUTION_MODE='services',IPDEMO_PRICE_SERVICE_URL=price_url,IPDEMO_EFFECT_SERVICE_URL=effect_url,IPDEMO_AUTH_ENABLED='0',IPDEMO_DEMO_READ_ONLY='0')
            app=Application(workspace)
        finally:
            for key,value in saved.items():
                if value is None:os.environ.pop(key,None)
                else:os.environ[key]=value
        core=DeepPriceAnalysis(app.store,app.model_gateway)
        def call(method,path,payload=None):
            status,result=AnalysisFeatures(core).handle(method,path,payload)
            if status!=200:raise RuntimeError('%s返回%d: %s'%(path,status,result.get('error')))
            return result
        schema=call('GET','/schema')
        report['field_count']=len(schema['fields'])
        products=call('GET','/products')['items']
        if not products:raise ValueError('当前已发布业务没有产品记录')
        known={f['field_id'] for f in schema['fields']}
        selected=products[:min(5,len(products))]
        request={k:schema[k] for k in IDENTITY}
        request['parameters']={k:v for k,v in selected[0]['parameters'].items() if k in known}
        data_before=app.store.export_json()
        delta=0
        for item in selected:
            params={k:v for k,v in item['parameters'].items() if k in known}
            deep=timed('single',lambda:call('POST','/predict',dict(request,parameters=params)))
            simple=app.price_workbench_predict({'parameters':params})
            encoded=app.store.runtime_parameters(app.store.canonical_business_parameters(params))
            recommendation=app.model_gateway.evaluate(encoded)
            delta=max(delta,abs(deep['predicted_price_wan']-simple['prediction']['predicted_price_wan']),
                      abs(deep['predicted_price_wan']-recommendation['predicted_price_wan']))
        if delta>1e-8:raise ValueError('推荐/简易/深度价格不一致')
        batch={k:request[k] for k in IDENTITY}
        batch['items']=[{'item_id':str(i),'parameters':{k:v for k,v in item['parameters'].items() if k in known}} for i,item in enumerate(selected)]
        price_fields=[f for f in schema['fields'] if f['price_model_input'] and f['type'] in ('number','integer')]
        if price_fields:
            missing=dict(request['parameters']);missing.pop(price_fields[0]['field_id'],None)
            report['checks']['missing_input_response_status']=core.handle('POST','/predict',dict(request,parameters=missing))[0]
        timed('batch',lambda:call('POST','/predict/batch',batch))
        timed('scenario',lambda:call('POST','/scenario',batch))
        for _ in range(repeats):
            top=timed('top10',lambda:call('POST','/sensitivity/top10',dict(request,field_ids=None)))
        report['top10']={k:top.get(k) for k in ('compared_field_count','sample_count','batch_count')}
        report['checks'].update(price_consistency_max_delta_wan=delta,records_compared=len(selected),read_only=app.store.export_json()==data_before)
        report['metadata']=call('GET','/model-summary')['data']
        report['checks']['model_identity']=schema['price_model_sha256']==sha(paths['price_model'])
        original_url=app.model_gateway.price_url
        def exercise_replacement(service,label):
            with serving(service) as changed_url:
                try:
                    app.model_gateway.price_url=changed_url
                    report['checks'][label+'_rejects_old_identity']=core.handle('POST','/predict',request)[0]==409
                    current=call('GET','/schema');fresh={k:current[k] for k in IDENTITY}
                    changed=call('POST','/predict',dict(fresh,parameters=request['parameters']))
                    report['checks'][label+'_fresh_prediction']=isinstance(changed['predicted_price_wan'],(int,float))
                finally:app.model_gateway.price_url=original_url
            report['checks'][label+'_recovery']=core.handle('POST','/predict',request)[0]==200
        exercise_replacement(PriceService(paths['price_model'],None,evaluation_metadata=metadata),'actual_service_restart')
        if replacement_build:
            other,other_paths,other_source,_=preflight(replacement_build,classification)
            if sha(other_source)!=sha(source) or other['product_code']!=manifest['product_code']:
                raise ValueError('替换模型须来自同一产品与同一输入表，避免用不兼容Schema代替模型替换测试')
            other_metadata=bind(other_paths['price_model'],other_paths['price_model'].parent/'training_report.json',output/'replacement_evaluation.local.json',classification)
            report['replacement_model_sha256']=sha(other_paths['price_model'])
            exercise_replacement(PriceService(other_paths['price_model'],None,evaluation_metadata=other_metadata),'actual_model_replacement')
            # Inject a real service switch only after the first completed core
            # batch; subsequent samples must not yield a mixed V3 curve.
            with serving(PriceService(other_paths['price_model'],None,evaluation_metadata=other_metadata)) as changed_url:
                original_handle=DeepPriceAnalysis.handle
                completed=[]
                def switch_after_batch(instance,method,path,*args,**kwargs):
                    status,value=original_handle(instance,method,path,*args,**kwargs)
                    if path=='/predict/batch' and status==200 and not completed:
                        completed.append(True);app.model_gateway.price_url=changed_url
                    return status,value
                try:
                    with patch.object(DeepPriceAnalysis,'handle',switch_after_batch):
                        status,value=AnalysisFeatures(core).handle('POST','/sensitivity/top10',dict(request,field_ids=None))
                    report['checks']['v3_cross_batch_replacement_rejected']=status==409 and 'series' not in value and bool(completed)
                finally:app.model_gateway.price_url=original_url
        try:
            app.model_gateway.price_url='http://127.0.0.1:1'
            report['checks']['unavailable_is_503']=core.handle('GET','/schema')[0]==503
        finally:app.model_gateway.price_url=original_url
        report['checks']['unavailable_recovery']=core.handle('POST','/predict',request)[0]==200
    report['checks']['original_artifacts_unchanged']=all(sha(paths[k])==v for k,v in before.items())
    if not all(v for k,v in report['checks'].items() if isinstance(v,bool)):
        raise ValueError('只读/身份/异常校验失败')
    report['status']='pass'
    report['formal_model_api_checks_passed']=classification=='formal' and manifest.get('virtual_data') is False
    report['acceptance_scope']='Programmatic consistency and inference timing only; browser/target-Win7/business-accuracy acceptance remain independent.'
    save()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--classification',choices=['formal','synthetic','unconfirmed'],default='unconfirmed')
    parser.add_argument('--repeats',type=int,default=3)
    parser.add_argument('--replacement-build',default=None)
    args=parser.parse_args()
    if not 1<=args.repeats<=10:parser.error('repeats范围1至10')
    report=verify(args.build_dir,args.output,args.classification,args.repeats,args.replacement_build)
    print(json.dumps({k:report[k] for k in ('status','classification','formal_acceptance','field_count','model','timings','checks')},ensure_ascii=True,indent=2))


if __name__=='__main__':main()
