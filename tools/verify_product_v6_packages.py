# -*- coding: utf-8 -*-
"""Exercise actual Windows BATs and fixed runtime, without source import fallback."""
import argparse,copy,csv,ctypes,hashlib,io,json,math,os,signal,socket,subprocess,sys,time,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v6_test import data_fixture
from tools.verify_product_v5_packages import extract

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def main():
    p=argparse.ArgumentParser();p.add_argument('--full',required=True);p.add_argument('--patch',required=True);p.add_argument('--report',required=True);p.add_argument('--tag',default='final');a=p.parse_args()
    evidence=Path(a.report).resolve().parent;evidence.mkdir(parents=True,exist_ok=True)
    archive_hashes=(sha(a.full),sha(a.patch))
    if not a.tag.replace('_','').isalnum():raise ValueError('Invalid acceptance tag')
    full=ROOT/('outputs/v6_accept_full_'+a.tag);patch=ROOT/('outputs/v6_accept_patch_'+a.tag);relocated=ROOT/('outputs/v6_accept_relocated_'+a.tag)
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='')
    def run(root,args,label,success=True):
        proc=subprocess.run(args,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=420,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=proc.stdout.decode('utf-8',errors='replace');(evidence/(label+'.log')).write_text(text,encoding='utf-8')
        if success and proc.returncode:raise AssertionError(label+': '+text[-6000:])
        if not success and not proc.returncode:raise AssertionError(label+' unexpectedly passed')
        return text
    def py(root,args,label):return run(root,[str(root/'runtime/python38/python.exe'),'-X','utf8']+args,label)
    def bat(root,name,label,*args,**kwargs):return run(root,['cmd.exe','/d','/c',name]+list(args),label,**kwargs)
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(url):
        with opener.open(url,timeout=20) as r:return json.loads(r.read().decode('utf-8'))
    def text(url):
        with opener.open(url,timeout=20) as r:return r.read().decode('utf-8')
    def post(url,payload):
        req=urllib.request.Request(url,data=json.dumps(payload).encode('utf-8'),headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=90) as r:return json.loads(r.read().decode('utf-8'))
    def current(root):
        out=root/read(root/'state/latest.json')['output'];return read(out/'price/training_report.json'),out
    from ctypes import wintypes as w
    kernel=ctypes.WinDLL('kernel32',use_last_error=True);kernel.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];kernel.OpenProcess.restype=w.HANDLE
    kernel.TerminateProcess.argtypes=[w.HANDLE,w.UINT];kernel.CloseHandle.argtypes=[w.HANDLE]
    def startup(root,label,rows,features,force=False,generation=False):
        before_pointer=(root/'state/latest.json').read_bytes();report,out=current(root)
        m=read(out/'prototype_manifest.json');files=[out/m[k] for k in ('price_model','effectiveness_model','business_package')]
        before=[(sha(f),f.stat().st_mtime_ns) for f in files];ready=None;handles=[];result={}
        with (evidence/(label+'.log')).open('w',encoding='utf-8') as log:
            proc=subprocess.Popen(['cmd.exe','/d','/c','START.bat','--no-browser','--price-port','0','--effect-port','0','--app-port','0','--cost-port','0'],
                cwd=str(root),env=env,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                start=time.time();deadline=time.monotonic()+60
                while time.monotonic()<deadline:
                    if proc.poll() is not None:raise AssertionError((evidence/(label+'.log')).read_text(encoding='utf-8'))
                    try:
                        state=read(out/'running.json')
                        if (out/'running.json').stat().st_mtime>=start and get(state['price_url']+'/health')['status']=='ok':ready=state;break
                    except Exception:pass
                    time.sleep(.1)
                if ready is None:raise AssertionError('START timed out: '+label)
                handles=[kernel.OpenProcess(0x0001|0x1000,False,pid) for pid in [ready['pid']]+ready['child_pids']]
                base=ready['url'].rstrip('/');b=get(base+'/api/bootstrap')
                assert b['counts']['historical']==rows and len(b['parameters'])==features
                index=text(base+'/');assert 'V21.6' in index and '优化目标' in index and 'railResizeHandle' in index
                assert 'initRailResize' in text(base+'/app.js')
                assert 'portalAnalysis' in text(ready['portal_url'])
                services={s['key']:s for s in get(base+'/api/portal')['services']}
                assert services['cost_effectiveness_analysis']['url']==ready['cost_effectiveness_url']
                cost=ready['cost_effectiveness_url'].rstrip('/');health=get(cost+'/health')
                assert health['database_readonly'] and health['database']=='ok' and health['price_service']=='ok' and health['effectiveness_service']=='ok'
                assert health['recommendation_url']==ready['portal_url']
                schemes=get(cost+'/api/schemes')['items'];assert len(schemes)>=2
                analysis=post(cost+'/api/analyze',{'scheme_ids':[s['scheme_id'] for s in schemes[:2]]})
                assert analysis['success'] and len(analysis['schemes'])==2
                query={'session_id':'v6-acceptance','source_mode':'historical','selected_tags':[],'indicator_filters':[],
                       'sort_by':'comprehensive','count':2,'generation_count':2,'page':1,'page_size':20,'start_generation':False}
                ranked=post(base+'/api/recommend',query);assert ranked['total']==rows
                response=post(base+'/api/price-workbench/predict',{'parameters':ranked['items'][0]['params']})
                prediction=response['prediction']['predicted_price_wan'];explanation=response['explanation']
                assert math.isfinite(prediction) and prediction>0
                assert abs(sum(x['weighted_contribution_wan'] for x in explanation['model_contributions'])-prediction)<1e-5
                if generation:
                    task=post(base+'/api/generation/request',query);deadline=time.monotonic()+100
                    while task['status'] not in ('completed','failed') and time.monotonic()<deadline:
                        time.sleep(.2);task=get(base+'/api/generation-tasks/'+task['task_id'])
                    assert task['status']=='completed',task
                result={'status':'PASS','rows':rows,'features':features,'recommendation_version':'V21.6','portal':True,'cost_analysis':True,
                        'force_parent_exit':force,'generation':generation,'price':prediction,'child_count':len(ready['child_pids'])}
            finally:
                if force and ready and handles and handles[0]:kernel.TerminateProcess(handles[0],1)
                elif proc.poll() is None:proc.send_signal(signal.CTRL_BREAK_EVENT)
                try:proc.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if handles and handles[0]:kernel.TerminateProcess(handles[0],1)
                    proc.wait(timeout=10)
                deadline=time.monotonic()+10
                if ready:
                    from urllib.parse import urlparse
                    ports=[urlparse(ready[k]).port for k in ('url','price_url','effectiveness_url','cost_effectiveness_url')]
                    while True:
                        open_ports=[]
                        for port in ports:
                            with socket.socket() as s:
                                s.settimeout(.2)
                                if s.connect_ex(('127.0.0.1',port))==0:open_ports.append(port)
                        if not open_ports:break
                        if time.monotonic()>deadline:raise AssertionError('Owned ports still alive: '+repr(open_ports))
                        time.sleep(.1)
                for handle in handles:
                    if handle:kernel.CloseHandle(handle)
        assert before_pointer==(root/'state/latest.json').read_bytes()
        assert before==[(sha(f),f.stat().st_mtime_ns) for f in files]
        result.update(no_retraining=True,all_four_ports_closed=True);return result
    extract(a.full,full);py(full,['tools/product_portable.py','verify','--full'],'full_manifest')
    starts=[startup(full,'full_start',16,36,generation=True)]
    switches=[]
    for selector in ('SELECT_V2','SELECT_V3','V4_RANDOM','V4_GLOBAL','V4_PER_SPLIT','V4_HYBRID'):
        bat(full,selector+'.bat','select_'+selector);assert json.loads(py(full,['tools/product_modes.py','status'],'status_'+selector))['saved'];switches.append(selector)
    starts.append(startup(full,'old_model_new_ui',16,36))
    for protocol,scope in [('legacy_split','selected_split'),('fixed_cv','full_data')]:
        for mode in ('goal_first','balanced','robust'):
            py(full,['tools/product_modes.py','select','--engine','optuna_v5','--protocol',protocol,'--scope',scope,'--strategy',mode],protocol+'_'+mode)
            assert json.loads(py(full,['tools/product_modes.py','status'],'status_'+protocol+'_'+mode))['saved'];switches.append(protocol+'_'+mode)
    for mode in ('balanced','robust','goal_first'):
        bat(full,'V6_'+mode.upper()+'.bat','select_v6_'+mode);assert json.loads(py(full,['tools/product_modes.py','status'],'status_v6_'+mode))['saved'];switches.append('v6_'+mode)
    (full/'input/products.xlsx').rename(full/'input/products.xlsx.disabled')
    starts.append(startup(full,'no_input_force_exit',16,36,force=True))
    from tools.build_product_v6 import base_archive
    extract(base_archive(),patch)
    old={p.relative_to(patch).as_posix():(sha(p),p.stat().st_mtime_ns) for p in (patch/'outputs').rglob('price_native_bundle.pkl')}
    input_before=sha(patch/'input/products.xlsx')
    with zipfile.ZipFile(a.patch) as z:
        info=json.loads(z.read('OPTUNA_V6_PATCH_FILES.json').decode('utf-8'))
        for row in info['files']:assert hashlib.sha256(z.read(row['path'])).hexdigest()==row['sha256']
        z.extractall(patch)
    bat(patch,'APPLY_V6.bat','patch_apply');assert input_before==sha(patch/'input/products.xlsx')
    assert old=={n:(sha(patch/n),(patch/n).stat().st_mtime_ns) for n in old}
    py(patch,['tools/product_portable.py','verify','--full'],'patch_manifest')
    bat(patch,'START.bat','patch_untrained','--no-browser',success=False)
    bat(patch,'APPLY_V6_DEEP.bat','patch_deep');deep=read(patch/'config/price_optuna_v6.json')
    assert deep['search']['n_trials']==120 and deep['v6']['budget']['max_member_fits']==6000
    bat(patch,'APPLY_V6_QUICK.bat','patch_quick');cfg=read(patch/'config/price_optuna_v6.json')
    assert cfg['search']['n_trials']==12 and cfg['v6']['budget']['max_member_fits']==700
    cfg['search'].update(n_trials=7,timeout_seconds=180);cfg['v6']['budget'].update(tiny_trials=7,max_member_fits=700)
    cfg['expert'].update(scores={'x0':5,'x1':4},field_types={'x%d'%i:('enum' if i in (3,4) else 'number') for i in range(40)})
    cfg['v6']['persistence'].update(enabled=True,directory='state/v6_acceptance');cfgpath=patch/'config/price_optuna_v6.json';write(cfgpath,cfg)
    (patch/'input/products.xlsx').rename(patch/'input/products.xlsx.disabled')
    def source(n):
        data=data_fixture(n,40);specs=data['parameters'];buf=io.StringIO(newline='');wri=csv.writer(buf)
        wri.writerow(['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs]);wri.writerow(['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs])
        for row in data['agreements']:wri.writerow([row['agreement_id'],'V6虚拟验收',row['historical_price_wan']]+[row['params'][p['parameter_id']] for p in specs])
        (patch/'input/products.csv').write_bytes(buf.getvalue().encode('utf-8-sig'))
        settings=read(patch/'config/portable.json');settings['demo_input_sha256']=sha(patch/'input/products.csv');write(patch/'config/portable.json',settings)
    cases=[]
    def train(label,n,mode='goal_first',command='RETRAIN.bat'):
        bat(patch,'V6_'+mode.upper()+'.bat',label+'_select');bat(patch,'CHECK_INPUT.bat',label+'_input');bat(patch,command,label+'_train')
        r,out=current(patch);assert r['training_rows']==n and not r['v3_used_for_selection'] and not r['weights_fitted_on_test']
        assert r['test_metrics']['rows']==n and r['native_equivalence_max_delta_wan']<1e-5
        assert not r.get('interval_uses_test_labels',False)
        events=[json.loads(s) for s in (out/'price/training_events.jsonl').read_text(encoding='utf-8').splitlines()]
        names={x['event'] for x in events}
        assert {'v6_input','v6_report_saved'}.issubset(names)
        if r['model_fits_this_session']:
            assert {'v6_member_fit_start','v6_outer_result'}.issubset(names)
        else:assert 'v6_outer_restored' in names and 'v6_member_fit_start' not in names
        cases.append({'label':label,'rows':n,'train':r['train_metrics'],'test':r['test_metrics'],'fits':r['model_fits_this_session'],'experts':r['expert_scores_enabled']});return r,out
    for n in range(4,8):source(n);train('tiny_'+str(n),n)
    r,last=train('resume_completed',7,command='RESUME.bat');assert r['model_fits_this_session']==0
    train('balanced',7,'balanced');train('robust',7,'robust')
    cfg=read(cfgpath);cfg['v6']['mode']='goal_first';cfg['expert']['enabled']=False;write(cfgpath,cfg);train('experts_off',7)
    before=(patch/'state/latest.json').read_bytes();registry=(patch/'state/modes.json').read_bytes()
    strict=read(cfgpath);strict['targets'].update(mape_percent_max=1e-12,r2_min=1.);strict['v6']['publish']['policy']='require_targets';write(cfgpath,strict)
    bat(patch,'RETRAIN.bat','strict_failure',success=False)
    assert before==(patch/'state/latest.json').read_bytes() and registry==(patch/'state/modes.json').read_bytes()
    write(cfgpath,cfg);bat(patch,'V6_GOAL_FIRST.bat','restore_mode');_,out=current(patch)
    price=out/read(out/'prototype_manifest.json')['price_model'];price_before=(sha(price),price.stat().st_mtime_ns)
    effect=read(out/read(out/'prototype_manifest.json')['effectiveness_model']);effect['manifest']['model_version']='expert-v6-acceptance'
    expert=patch/'expert_effect.json';write(expert,effect);bat(patch,'IMPORT_EFFECTIVENESS.bat','import_expert','--source',str(expert))
    assert price_before==(sha(price),price.stat().st_mtime_ns)
    assert read(out/'prototype_manifest.json')['effectiveness_backend']=='expert_snapshot'
    starts.append(startup(patch,'imported_effect_start',7,40,force=True))
    saved=evidence/'Saved_V6_Acceptance.zip';bat(patch,'EXPORT.bat','export_saved','--output',str(saved))
    extract(saved,relocated);assert not (relocated/'input/products.csv').exists()
    starts.append(startup(relocated,'relocated_saved_start',7,40,force=True))
    assert archive_hashes==(sha(a.full),sha(a.patch)),'Archives changed during acceptance'
    result={'status':'PASS','full_sha256':archive_hashes[0],'patch_sha256':archive_hashes[1],'switches':switches,'saved_mode_count':len(switches),
        'preserved_old_models':len(old),'input_preserved_by_patch':True,'actual_bat_training_cases':cases,'strict_failure_preserves_latest':True,
        'expert_effect_import_price_unchanged':True,'starts':starts,'saved_export_without_input':True,'source_import_fallback':False,
        'business_accuracy_certified':False,'runtime':'Python3.8.10/sklearn0.24.2','physical_win7_tested':False}
    write(a.report,result);print(json.dumps(result,ensure_ascii=False))

if __name__=='__main__':main()
