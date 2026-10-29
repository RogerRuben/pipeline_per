# -*- coding: utf-8 -*-
"""Verify real release archives/BATs with their own runtime, then relocate EXPORT."""
import argparse,copy,ctypes,hashlib,json,os,signal,socket,subprocess,sys,time,urllib.request,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v6_test import data_fixture
from product_optuna_v2_test import workbook
from tools.verify_product_v5_packages import extract
from tools.build_product_v7 import base_archive

def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def read(path):return json.loads(Path(path).read_text(encoding='utf-8-sig'))
def write(path,value):Path(path).write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')

def main():
    p=argparse.ArgumentParser();p.add_argument('--full',required=True);p.add_argument('--patch',required=True);p.add_argument('--report',required=True);p.add_argument('--tag',default='release');a=p.parse_args()
    evidence=Path(a.report).resolve().parent/('package_'+a.tag);evidence.mkdir(parents=True,exist_ok=True)
    if not a.tag.replace('_','').isalnum():raise ValueError('Invalid tag')
    full=ROOT/('outputs/v7_accept_full_'+a.tag);patch=ROOT/('outputs/v7_accept_patch_'+a.tag);relocated=ROOT/('outputs/v7_accept_relocated_'+a.tag)
    hashes=(sha(a.full),sha(a.patch));env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='')
    def run(root,args,label,success=True):
        pr=subprocess.run(args,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=480,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=pr.stdout.decode('utf-8',errors='replace');(evidence/(label+'.log')).write_text(text,encoding='utf-8')
        if success and pr.returncode:raise AssertionError(label+': '+text[-7000:])
        if not success and not pr.returncode:raise AssertionError(label+' unexpectedly succeeded')
        return text
    def py(root,args,label):return run(root,[str(root/'runtime/python38/python.exe'),'-X','utf8']+args,label)
    def bat(root,name,label,*args,**kw):return run(root,['cmd.exe','/d','/c',name]+list(args),label,**kw)
    def current(root):
        out=root/read(root/'state/latest.json')['output'];return read(out/'price/training_report.json'),out
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    def get(url):
        with opener.open(url,timeout=20) as r:return json.loads(r.read().decode('utf-8'))
    def text(url):
        with opener.open(url,timeout=20) as r:return r.read().decode('utf-8')
    def post(url,data):
        req=urllib.request.Request(url,data=json.dumps(data).encode('utf-8'),headers={'Content-Type':'application/json'})
        with opener.open(req,timeout=90) as r:return json.loads(r.read().decode('utf-8'))
    from ctypes import wintypes as w
    k=ctypes.WinDLL('kernel32',use_last_error=True);k.OpenProcess.argtypes=[w.DWORD,w.BOOL,w.DWORD];k.OpenProcess.restype=w.HANDLE
    k.TerminateProcess.argtypes=[w.HANDLE,w.UINT];k.CloseHandle.argtypes=[w.HANDLE]
    def startup(root,label,n,p,force=False,grade=False):
        r,out=current(root);m=read(out/'prototype_manifest.json');paths=[out/m[key] for key in ('price_model','effectiveness_model','business_package')]
        before=[(sha(x),x.stat().st_mtime_ns) for x in paths];pointer=(root/'state/latest.json').read_bytes();info=None;handles=[]
        with (evidence/(label+'.log')).open('w',encoding='utf-8') as log:
            pr=subprocess.Popen(['cmd.exe','/d','/c','START.bat','--no-browser','--price-port','0','--effect-port','0','--app-port','0','--cost-port','0'],cwd=str(root),env=env,
                stdin=subprocess.DEVNULL,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
            try:
                start=time.time();deadline=time.monotonic()+65
                while time.monotonic()<deadline:
                    if pr.poll() is not None:raise AssertionError((evidence/(label+'.log')).read_text(encoding='utf-8'))
                    try:
                        state=read(out/'running.json')
                        if (out/'running.json').stat().st_mtime>=start and get(state['price_url']+'/health')['status']=='ok':info=state;break
                    except Exception:pass
                    time.sleep(.1)
                if info is None:raise AssertionError('Saved START timeout')
                handles=[k.OpenProcess(0x1000|0x1,False,pid) for pid in [info['pid']]+info['child_pids']]
                base=info['url'].rstrip('/');b=get(base+'/api/bootstrap');assert b['counts']['historical']==n and len(b['parameters'])==p
                assert 'V21.6' in text(base+'/') and '优化目标' in text(base+'/') and 'initRailResize' in text(base+'/app.js')
                portal=get(base+'/api/portal');assert any(s['key']=='cost_effectiveness_analysis' for s in portal['services'])
                cost=info['cost_effectiveness_url'].rstrip('/');health=get(cost+'/health');assert health['database']=='ok' and health['recommendation_url']==info['portal_url']
                schemes=get(cost+'/api/schemes')['items'];analysis=post(cost+'/api/analyze',{'scheme_ids':[s['scheme_id'] for s in schemes[:2]]});assert analysis['success']
                business=read(out/'business_release.iprelease.json');data=business.get('data') or business.get('release',{}).get('data');sample=data['agreements'][0]['params']
                response=post(base+'/api/price-workbench/predict',{'parameters':sample});explanation=response['explanation'];pred=response['prediction']['predicted_price_wan']
                assert abs(sum(x['weighted_contribution_wan'] for x in explanation['model_contributions'])-pred)<1e-5
                if r['engine']=='optuna_v7':
                    assert explanation['protocol']=='v3plus_common_development' and explanation['test_metrics']['r2'] is None
                    assert 'anchor_development_metrics' in explanation
                    if grade:assert explanation['expert_grade_consistency']['mode']=='report'
                result={'rows':n,'features':p,'force_parent_exit':force,'all_four_ports_closed':True,'no_retraining':True,'portal':True,'cost_analysis':True,'grade_api':grade}
            finally:
                if force and handles and handles[0]:k.TerminateProcess(handles[0],1)
                elif pr.poll() is None:pr.send_signal(signal.CTRL_BREAK_EVENT)
                try:pr.wait(timeout=20)
                except subprocess.TimeoutExpired:
                    if handles and handles[0]:k.TerminateProcess(handles[0],1)
                    pr.terminate();pr.wait(timeout=10)
                if info:
                    from urllib.parse import urlparse
                    ports=[urlparse(info[key]).port for key in ('url','price_url','effectiveness_url','cost_effectiveness_url')];deadline=time.monotonic()+10
                    while True:
                        alive=[]
                        for port in ports:
                            with socket.socket() as s:
                                s.settimeout(.2)
                                if s.connect_ex(('127.0.0.1',port))==0:alive.append(port)
                        if not alive:break
                        if time.monotonic()>deadline:raise AssertionError('Owned ports still listening: '+str(alive))
                        time.sleep(.1)
                for h in handles:
                    if h:k.CloseHandle(h)
        assert before==[(sha(x),x.stat().st_mtime_ns) for x in paths] and pointer==(root/'state/latest.json').read_bytes()
        return result
    extract(a.full,full);py(full,['tools/product_portable.py','verify','--full'],'full_manifest');starts=[startup(full,'full_start',16,36)]
    modes=[]
    for selector in ('SELECT_V2','SELECT_V3','V4_RANDOM','V4_GLOBAL','V4_PER_SPLIT','V4_HYBRID'):
        bat(full,selector+'.bat',selector);assert json.loads(py(full,['tools/product_modes.py','status'],selector+'_status'))['saved'];modes.append(selector)
    for protocol,scope in [('legacy_split','selected_split'),('fixed_cv','full_data')]:
        for mode in ('goal_first','balanced','robust'):
            py(full,['tools/product_modes.py','select','--engine','optuna_v5','--protocol',protocol,'--scope',scope,'--strategy',mode],protocol+'_'+mode)
            assert json.loads(py(full,['tools/product_modes.py','status'],protocol+'_'+mode+'_status'))['saved'];modes.append(protocol+'_'+mode)
    for version,choices in [(6,['goal_first','balanced','robust']),(7,['balanced','goal_first','baseline_only'])]:
        for mode in choices:
            bat(full,'V%d_%s.bat'%(version,mode.upper()),'v%d_%s'%(version,mode));assert json.loads(py(full,['tools/product_modes.py','status'],'status_v%d_%s'%(version,mode)))['saved'];modes.append('v%d_%s'%(version,mode))
    bat(full,'V7_BALANCED.bat','restore_v7');bat(full,'COMPARE_V3_PLUS.bat','read_comparison')
    (full/'input/products.xlsx').rename(full/'input/products.xlsx.disabled');starts.append(startup(full,'no_input_force_exit',16,36,force=True))
    extract(base_archive(),patch);old={x.relative_to(patch).as_posix():(sha(x),x.stat().st_mtime_ns) for x in (patch/'outputs').rglob('price_native_bundle.pkl')};old_input=sha(patch/'input/products.xlsx')
    with zipfile.ZipFile(a.patch) as z:
        manifest=json.loads(z.read('OPTUNA_V7_PATCH_FILES.json').decode('utf-8'))
        for f in manifest['files']:assert hashlib.sha256(z.read(f['path'])).hexdigest()==f['sha256']
        z.extractall(patch)
    bat(patch,'APPLY_V7.bat','apply_v7');assert old_input==sha(patch/'input/products.xlsx') and old=={n:(sha(patch/n),(patch/n).stat().st_mtime_ns) for n in old}
    py(patch,['tools/product_portable.py','verify','--full'],'patch_manifest');bat(patch,'START.bat','untrained','--no-browser',success=False)
    bat(patch,'APPLY_V7_DEEP.bat','deep');c=read(patch/'config/price_optuna_v7.json');assert c['search']['n_trials']==5000 and c['v7']['budget']['max_extra_fits']==1000
    bat(patch,'APPLY_V7_QUICK.bat','quick');c=read(patch/'config/price_optuna_v7.json');assert c['search']['n_trials']==40
    c['search'].update(n_trials=3,timeout_seconds=120);c['v7']['budget'].update(max_extra_fits=160,extra_seconds=120);c['v7']['model_seeds']['count']=2
    c['expert']['field_types']={'x%d'%i:('enum' if i in (3,4) else 'number') for i in range(40)};cfgpath=patch/'config/price_optuna_v7.json';write(cfgpath,c)
    cases=[]
    def train(label,n,command='RETRAIN.bat'):
        bat(patch,'CHECK_INPUT.bat',label+'_input');bat(patch,command,label+'_train');r,out=current(patch)
        assert r['engine']=='optuna_v7' and r['training_rows']==n and r['native_equivalence_max_delta_wan']<1e-5 and not r['independent_test_provided']
        assert (out/'price/baseline_v3/price_native_bundle.pkl').is_file()
        cases.append({'label':label,'rows':n,'baseline_new_fits':r['baseline_new_fits'],'extra_fits':r['extra_fits_this_session'],'consistency':r['expert_grade_consistency'],'selected':r['selected_model']});return r,out
    for n in range(4,8):
        source=patch/'input/products.xlsx';source.write_bytes(workbook(data_fixture(n,40)))
        settings=read(patch/'config/portable.json');settings['demo_input_sha256']=sha(source);write(patch/'config/portable.json',settings)
        train('tiny_'+str(n),n)
    r,out=train('resume',7,'RESUME.bat');assert r['baseline_new_fits']==0 and r['extra_fits_this_session']==0
    bat(patch,'V7_EXPERT_REPORT.bat','expert_report_select');r,out=train('expert_report',7);assert r['expert_grade_consistency']['mode']=='report'
    starts.append(startup(patch,'grade_start',7,40,force=True,grade=True));bat(patch,'COMPARE_V3_PLUS.bat','comparison_saved')
    before=(patch/'state/latest.json').read_bytes();registry=(patch/'state/modes.json').read_bytes()
    bat(patch,'V7_EXPERT_STRICT.bat','strict_select');strict=read(cfgpath);strict['v7']['publish']['policy']='require_targets';write(cfgpath,strict)
    bat(patch,'RETRAIN.bat','strict_failure',success=False)
    assert before==(patch/'state/latest.json').read_bytes() and registry==(patch/'state/modes.json').read_bytes()
    strict['v7']['publish']['policy']='allow_draft';write(cfgpath,strict)
    bat(patch,'V7_EXPERT_REPORT.bat','restore_report');r,out=current(patch)
    m=read(out/'prototype_manifest.json');price=out/m['price_model'];price_before=(sha(price),price.stat().st_mtime_ns)
    effect=read(out/m['effectiveness_model']);effect['manifest']['model_version']='expert-v7-package-acceptance';file=patch/'expert_effect.json';write(file,effect)
    bat(patch,'IMPORT_EFFECTIVENESS.bat','import_effect','--source',str(file));assert price_before==(sha(price),price.stat().st_mtime_ns)
    starts.append(startup(patch,'effect_import_start',7,40,force=True,grade=True))
    saved=evidence/'Saved_V7_Validated.zip';bat(patch,'EXPORT.bat','export','--output',str(saved));extract(saved,relocated)
    starts.append(startup(relocated,'relocated_start',7,40,force=True,grade=True))
    assert hashes==(sha(a.full),sha(a.patch))
    result={'status':'PASS','full_sha256':hashes[0],'patch_sha256':hashes[1],'saved_modes':modes,'mode_count':len(modes),'preserved_old_models':len(old),
        'input_preserved_by_patch':True,'starts':starts,'bat_training_cases':cases,'strict_failure_preserves_latest':True,
        'expert_effect_import_price_unchanged':True,'saved_export_without_input':True,'source_import_fallback':False,'business_accuracy_certified':False,'physical_win7_tested':False}
    write(a.report,result);print(json.dumps({'status':'PASS','modes':len(modes),'training_cases':len(cases),'starts':len(starts)},ensure_ascii=False))
if __name__=='__main__':main()
