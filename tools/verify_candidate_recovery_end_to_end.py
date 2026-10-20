# -*- coding: utf-8 -*-
import json,os,shutil,signal,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tools.verify_price_input_pipeline import get,post


def exercise(base):
    boot=get(base+'/api/bootstrap')
    assert boot['counts']['historical']==10 and len(boot['parameters'])==50 and boot['integration']['product_ready']
    payload={'session_id':'candidate-recovery','source_mode':'historical','selected_tags':[],'indicator_filters':[],
        'sort_by':'comprehensive','generation_count':1,'count':2,'page':1,'page_size':10,'start_generation':False}
    ranked=post(base,'/api/recommend',payload);assert ranked['items'];params=ranked['items'][0]['params']
    before=post(base,'/api/evaluate',{'parameters':params})
    after=post(base,'/api/evaluate',{'parameters':dict(params,rated_thrust_n=params['rated_thrust_n']+500)})
    assert abs(before['predicted_price_wan']-after['predicted_price_wan'])>1e-4 and len(after['coupling_assessments'])==3
    detail=post(base,'/api/price-workbench/predict',{'parameters':params})
    assert len(detail['explanation']['price_attribute_scores'])==50
    assert all(row['family']!='tabpfn' for row in detail['explanation']['model_contributions'])
    task=post(base,'/api/generation/request',payload);deadline=time.monotonic()+90
    while task['status'] not in ('completed','failed') and time.monotonic()<deadline:
        time.sleep(.25);task=get(base+'/api/generation-tasks/'+task['task_id'])
    assert task['status']=='completed',task
    generated=post(base,'/api/recommend',dict(payload,source_mode='generated',generation_batch_id=task['batch_id']))
    assert generated['total']>0
    return {'history':10,'attributes':50,'couplings':3,'price_recalculated':True,'generated':generated['total'],'no_failed_neural_model_used':True}


def main():
    output=ROOT/'outputs/candidate_failure_fix_20261015/negative_neural_pipeline'
    state=json.loads((output/'running.json').read_text(encoding='utf-8'))
    health=get(state['price_url']+'/health')
    assert health['status']=='ok' and not health['tabpfn_used_in_ensemble']
    assert len(get(state['price_url']+'/api/v1/schema')['fields'])==50
    host=exercise(state['url'].rstrip('/'))
    executable=ROOT/'outputs/旧包增量验证 20261014/TabPFN_WIN7_LAN_CLIENT_OFFLINE/runtime/tabpfn38/python.exe'
    script=executable.parents[2]/'tools/run_tabpfn_win7_client.py'
    with tempfile.TemporaryDirectory(prefix='recovered_client_',dir=str(ROOT/'outputs/candidate_failure_fix_20261015')) as directory:
        directory=Path(directory);assert ROOT/'outputs' in directory.resolve().parents
        copied=directory/'prototype'
        shutil.copytree(output,copied,ignore=shutil.ignore_patterns('price','running.json','client_running.json','*.log'))
        with (ROOT/'logs/candidate_recovery_python38_client_20261015.log').open('w',encoding='utf-8') as log:
            process=subprocess.Popen([str(executable),str(script),'--output',str(copied),'--price-url',state['price_url'],
                '--app-port','0','--effect-port','0','--no-browser'],cwd=str(script.parents[1]),stdout=log,stderr=log,
                env=dict(os.environ,PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1'),creationflags=getattr(subprocess,'CREATE_NEW_PROCESS_GROUP',0))
            try:
                deadline=time.monotonic()+60
                while not (copied/'client_running.json').exists():
                    assert process.poll() is None,'Client failed to start'
                    assert time.monotonic()<deadline,'Client timeout'
                    time.sleep(.2)
                client_state=json.loads((copied/'client_running.json').read_text(encoding='utf-8'))
                client=exercise(client_state['url'].rstrip('/'))
            finally:
                if process.poll() is None:
                    process.send_signal(signal.CTRL_BREAK_EVENT if os.name=='nt' else signal.SIGINT)
                    try:process.wait(timeout=8)
                    except subprocess.TimeoutExpired:process.terminate();process.wait(timeout=5)
    report={'status':'PASS','real_negative_candidate_injection':True,'host':host,'python38_client':client,'health_truthfully_reports_no_neural_member':True,'win7_os_verified':False}
    (ROOT/'outputs/candidate_failure_fix_20261015/full_chain_report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))

if __name__=='__main__':main()