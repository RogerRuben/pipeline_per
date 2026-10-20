# -*- coding: utf-8 -*-
import json,math,os,shutil,signal,subprocess,sys,tempfile,time,urllib.request
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.historical_onboarding import HistoricalProductOnboarding
from services.price_service.native_bundle import load_bundle,predict,predict_batch


def get(url):
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url,timeout=120) as response:
        return json.loads(response.read().decode('utf-8'))

def post(base,route,payload):
    request=urllib.request.Request(base.rstrip('/')+route,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request,timeout=180) as response:
        return json.loads(response.read().decode('utf-8'))

def exercise(base):
    boot=get(base+'/api/bootstrap')
    assert boot['counts']['historical']==10 and len(boot['parameters'])==50
    assert boot['integration']['product_ready']
    payload={'session_id':'price-input-fix-e2e','source_mode':'historical','selected_tags':[],'indicator_filters':[],
             'sort_by':'comprehensive','generation_count':1,'count':2,'page':1,'page_size':10,'start_generation':False}
    ranked=post(base,'/api/recommend',payload);assert ranked['items']
    params=ranked['items'][0]['params']
    before=post(base,'/api/evaluate',{'parameters':params})
    after=post(base,'/api/evaluate',{'parameters':dict(params,rated_thrust_n=params['rated_thrust_n']+500)})
    assert abs(before['predicted_price_wan']-after['predicted_price_wan'])>1e-4
    assert len(after['coupling_assessments'])==3
    detail=post(base,'/api/price-workbench/predict',{'parameters':params})
    scores=detail['explanation']['price_attribute_scores'];assert len(scores)==50
    inactive=next(row for row in scores if row['field_id']=='sample_rate_hz')
    assert inactive['score']==1 and not inactive['used_in_training']
    assert '整列为空' in inactive['exclusion_reason']
    task=post(base,'/api/generation/request',payload)
    deadline=time.monotonic()+90
    while task['status'] not in ('completed','failed') and time.monotonic()<deadline:
        time.sleep(.25);task=get(base+'/api/generation-tasks/'+task['task_id'])
    assert task['status']=='completed',task
    generated=post(base,'/api/recommend',dict(payload,source_mode='generated',generation_batch_id=task['batch_id']))
    assert generated['total']>0
    return {'history':10,'declared_attributes':50,'score_records':len(scores),'inactive_score_retained':True,'couplings':3,
            'recalculation_changed_price':True,'generated_recommendations':generated['total']}


def main():
    output=ROOT/'outputs/price_input_fix_20261015/trained_combined'
    running=json.loads((output/'running.json').read_text(encoding='utf-8'))
    source=ROOT/'outputs/price_input_fix_20261015/workbooks/copied_combined.xlsx'
    data=HistoricalProductOnboarding().analyze(source.name,source.read_bytes(),'PASTE_INPUT','复制粘贴导入验证',
            ['-1','\\','/'],require_dual_header=True)['data']
    bundle=load_bundle(output/'price/price_native_bundle.pkl')
    params=[row['params'] for row in data['agreements']]
    batch=predict_batch(bundle,params)
    for i in (0,2,5):
        direct=predict(bundle,params[i])
        served=post(running['price_url'],'/api/v1/predict',{'parameters':params[i]})
        assert direct['predicted_price_wan']==served['prediction']['predicted_price_wan']
        assert abs(direct['predicted_price_wan']-batch[i]['predicted_price_wan'])<1e-4
    schema=get(running['price_url']+'/api/v1/schema')
    assert len(schema['fields'])==49
    host=exercise(running['url'].rstrip('/'))
    client_python=ROOT/'outputs/旧包增量验证 20261014/TabPFN_WIN7_LAN_CLIENT_OFFLINE/runtime/tabpfn38/python.exe'
    client_script=client_python.parents[2]/'tools/run_tabpfn_win7_client.py'
    with tempfile.TemporaryDirectory(prefix='client_input_fix_',dir=str(ROOT/'outputs/price_input_fix_20261015')) as temp:
        temp=Path(temp);assert ROOT/'outputs' in temp.resolve().parents
        copied=temp/'prototype'
        shutil.copytree(output,copied,ignore=shutil.ignore_patterns('price','running.json','client_running.json','*.log'))
        env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1')
        logfile=ROOT/'logs/price_input_python38_client_20261015.log'
        with logfile.open('w',encoding='utf-8') as log:
            child=subprocess.Popen([str(client_python),str(client_script),'--output',str(copied),'--price-url',running['price_url'],
                '--app-port','0','--effect-port','0','--no-browser'],cwd=str(client_script.parents[1]),env=env,stdout=log,stderr=log,
                creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
            try:
                deadline=time.monotonic()+60
                while not (copied/'client_running.json').exists():
                    assert child.poll() is None,logfile.read_text(encoding='utf-8')
                    assert time.monotonic()<deadline,'Python3.8 client startup timed out'
                    time.sleep(.2)
                state=json.loads((copied/'client_running.json').read_text(encoding='utf-8'))
                legacy=exercise(state['url'].rstrip('/'))
            finally:
                if child.poll() is None:
                    child.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
                    try:child.wait(timeout=8)
                    except subprocess.TimeoutExpired:child.terminate();child.wait(timeout=5)
    result={'status':'PASS','native_batch_reload_http_equivalence':True,'price_schema_features':49,'host':host,'python38_client':legacy,
            'bundle_sha256_verified_by_client':True,'win7_os_verified':False}
    report=ROOT/'outputs/price_input_fix_20261015/new_bundle_full_chain_report.json'
    report.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()