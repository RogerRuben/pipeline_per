# -*- coding: utf-8 -*-
"""Exercise actual full/PATCH BATs, saved scopes, telemetry and cold relocated export."""
import argparse,csv,hashlib,io,json,os,shutil,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v3_test import mixed_fixture
from tools.verify_product_r4_portable import exercise


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def extract(path,root):
    if root.exists():raise ValueError('Verification directory must be new: '+str(root))
    root.mkdir();shutil.copytree(ROOT/'deliverables/product_r41_portable/ProductAutoML_R4_1_20261013/runtime',root/'runtime',copy_function=os.link,ignore=shutil.ignore_patterns('__pycache__'))
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        for name in z.namelist():
            rel='/'.join(name.split('/')[1:])
            if not rel or rel.startswith('runtime/'):continue
            out=root/rel;out.parent.mkdir(parents=True,exist_ok=True);out.write_bytes(z.read(name))


def main():
    p=argparse.ArgumentParser();p.add_argument('--full',required=True);p.add_argument('--patch',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    evidence=Path(a.report).resolve().parent;full=ROOT/'outputs/v5f';patch=ROOT/'outputs/v5p';relocated=ROOT/'outputs/v5x'
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='')
    def run(root,args,label,success=True):
        r=subprocess.run(args,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=360,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=r.stdout.decode('utf-8',errors='replace');(evidence/(label+'.log')).write_text(text,encoding='utf-8')
        if success and r.returncode:raise AssertionError(label+': '+text[-8000:])
        if not success and not r.returncode:raise AssertionError(label+' unexpectedly passed')
        return text
    def py(root,args,label):return run(root,[str(root/'runtime/python38/python.exe'),'-X','utf8']+args,label)
    def bat(root,name,label,*args,**kwargs):return run(root,['cmd.exe','/d','/c',name]+list(args),label,**kwargs)
    extract(a.full,full);py(full,['tools/product_portable.py','verify','--full'],'full_manifest')
    full_start=exercise(full,evidence/'full_saved_start.log',generation=True,expected_rows=16,expected_features=36,change_field='x1',change_delta=5.)
    switches=[]
    for name in ('SELECT_V2','SELECT_V3','V4_RANDOM','V4_GLOBAL','V4_PER_SPLIT','V4_HYBRID','V5_BALANCED','V5_ROBUST','V5_GOAL_FIRST'):
        bat(full,name+'.bat','full_select_'+name);status=json.loads(py(full,['tools/product_modes.py','status'],'full_status_'+name))
        assert status['saved'];switches.append({'selector':name,'output':status['saved']['output']})
    (full/'input/products.xlsx').rename(full/'input/products.xlsx.disabled')
    no_input=exercise(full,evidence/'full_no_input_start.log',generation=False,expected_rows=16,expected_features=36,change_field='x1',change_delta=5.)
    from tools.build_product_v5 import base_archive
    extract(base_archive(),patch)
    old={p.relative_to(patch).as_posix():(sha(p),p.stat().st_mtime_ns) for p in (patch/'outputs').rglob('price_native_bundle.pkl')}
    with zipfile.ZipFile(a.patch) as z:
        info=json.loads(z.read('OPTUNA_V5_PATCH_FILES.json').decode('utf-8'))
        for row in info['files']:assert hashlib.sha256(z.read(row['path'])).hexdigest()==row['sha256']
        z.extractall(patch)
    bat(patch,'APPLY_V5.bat','patch_apply');assert old=={n:(sha(patch/n),(patch/n).stat().st_mtime_ns) for n in old}
    py(patch,['tools/product_portable.py','verify','--full'],'patch_manifest')
    bat(patch,'START.bat','patch_untrained_mode','--no-browser',success=False)
    bat(patch,'APPLY_V5_DEEP.bat','patch_deep_config')
    deep=json.loads((patch/'config/price_optuna_v5.json').read_text(encoding='utf-8'));assert deep['search']['n_trials']==300 and deep['v5']['budget']['max_member_fits']==5000 and deep['search']['timeout_seconds']==1800
    (patch/'input/products.xlsx').rename(patch/'input/products.xlsx.disabled')
    cfg=json.loads((full/'config/price_optuna_v5.json').read_text(encoding='utf-8'));cfg['search']['n_trials']=3
    cfg['expert']['scores']={'x0':5,'x4':4};cfg['expert']['field_types']={p['parameter_id']:('enum' if p['parameter_id'] in ('x3','x4') else 'number') for p in mixed_fixture(7,40)['parameters']};cfg['v5']['audit']['max_splits']=2
    cfgpath=patch/'config/price_optuna_v5.json'
    def write_cfg(value):cfgpath.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    write_cfg(cfg)
    def source(n):
        data=mixed_fixture(n,40);specs=data['parameters'];text=io.StringIO(newline='');w=csv.writer(text)
        w.writerow(['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs]);w.writerow(['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs])
        for row in data['agreements']:w.writerow([row['agreement_id'],'V5软件验证',row['historical_price_wan']]+[row['params'][p['parameter_id']] for p in specs])
        path=patch/'input/products.csv';path.write_bytes(text.getvalue().encode('utf-8-sig'));settings=patch/'config/portable.json';s=json.loads(settings.read_text(encoding='utf-8'));s['demo_input_sha256']=sha(path);settings.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding='utf-8')
    def current_report():
        pointer=json.loads((patch/'state/latest.json').read_text(encoding='utf-8'));return json.loads((patch/pointer['output']/'price/training_report.json').read_text(encoding='utf-8')),pointer
    cases=[]
    def train(label,n,mode='goal_first',command='RETRAIN.bat'):
        bat(patch,'V5_'+mode.upper()+'.bat',label+'_select');bat(patch,'CHECK_INPUT.bat',label+'_input');text=bat(patch,command,label+'_train')
        r,ptr=current_report();assert r['training_rows']==n and r['feature_count']==40 and r['native_equivalence_max_delta_wan']<1e-5 and r['search_mode']==mode
        cases.append({'label':label,'records':n,'mode':mode,'output':ptr['output'],'new_fits':r['new_member_fits'],'session_fits':r['model_fits_this_session'],'experts':r['expert_scores_enabled'],'scope':r['deployment_scope']});return r,text
    for n in range(4,8):
        source(n);r,_=train('tiny_%d'%n,n)
        if n==4:exercise(patch,evidence/'patch_four_record_start.log',generation=True,expected_rows=4,expected_features=40,change_field='x1',change_delta=5.)
    for mode in ('balanced','robust'):train('mode_'+mode,7,mode)
    # Actual V2/V3 BATs expose training metrics and keep their existing search.
    telemetry=[]
    for version in ('v2','v3'):
        path=patch/('config/price_optuna_'+version+'.json');c=json.loads(path.read_text(encoding='utf-8'));c['search']['n_trials']=2;c['expert']['scores']={'x0':5,'x4':4};c['expert']['field_types']=cfg['expert']['field_types']
        if version=='v3':c['v3']['warm_start']['auto_history']=False
        path.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8');bat(patch,'SELECT_'+version.upper()+'.bat','telemetry_'+version+'_select')
        text=bat(patch,'RETRAIN.bat','telemetry_'+version+'_train');assert '训练MAPE' in text and '开发MAPE' in text;telemetry.append(version)
    pure=json.loads(json.dumps(cfg));pure['expert']['enabled']=False;pure['v5']['train_gate']['mode']='off';pure['v5']['fusion']['enabled']=False;pure['deployment']['model_scope']='full_data';write_cfg(pure)
    r,_=train('pure_full',7);assert not r['expert_scores_enabled'] and r['deployment_scope']=='full_data'
    resume=json.loads(json.dumps(cfg));resume['v5']['audit']['enabled']=False;resume['v5']['enhancement'].update(max_new_fits=2,continue_after_targets=True);write_cfg(resume)
    r,_=train('resume_first',7);assert r['new_member_fits']==2
    resume['v5']['enhancement']['max_new_fits']=4;write_cfg(resume);r,_=train('resume_second',7,command='RESUME.bat');assert r['checkpoint_restored'] and r['new_member_fits']==4
    stopping=json.loads(json.dumps(cfg));stopping['targets'].update(r2_min=-1e6,mape_percent_max=100.);stopping['v5']['train_gate']['mode']='off';stopping['v5']['enhancement']['max_new_fits']=5000;stopping['v5']['audit']['enabled']=False;write_cfg(stopping)
    r,_=train('targets_stop',7);assert r['new_member_fits']==0 and r['stop_reason']=='targets_met_after_baseline'
    before=(patch/'state/latest.json').read_bytes();registry=(patch/'state/modes.json').read_bytes()
    strict=json.loads(json.dumps(cfg));strict['search']['n_trials']=1;strict['targets'].update(r2_min=1.,mape_percent_max=1e-12);strict['v5']['train_gate'].update(mode='hard',r2_min=1.,mape_percent_max=1e-12);strict['v5']['fusion']['enabled']=False;strict['v5']['audit']['enabled']=False;strict['v5']['publish']['policy']='require_targets';write_cfg(strict)
    bat(patch,'RETRAIN.bat','strict_failure',success=False);assert before==(patch/'state/latest.json').read_bytes() and registry==(patch/'state/modes.json').read_bytes()
    write_cfg(cfg);bat(patch,'V5_GOAL_FIRST.bat','restore_saved_goal');(patch/'input/products.csv').rename(patch/'input/products.csv.disabled')
    saved=exercise(patch,evidence/'patch_saved_without_input.log',generation=False,expected_rows=7,expected_features=40,change_field='x1',change_delta=5.)
    export=ROOT/'outputs/v5_saved_acceptance.zip'
    if export.exists():raise ValueError('Choose a new export path')
    bat(patch,'EXPORT.bat','export_saved','--output',str(export))
    with zipfile.ZipFile(export) as z:assert not any('/input/' in name for name in z.namelist()) and any('/automl_pipeline/product_optuna_v5_adapter.py' in name for name in z.namelist())
    extract(export,relocated);relocated_start=exercise(relocated,evidence/'relocated_saved_start.log',generation=False,expected_rows=7,expected_features=40,change_field='x1',change_delta=5.)
    result={'status':'PASS','full_sha256':sha(a.full),'patch_sha256':sha(a.patch),'full_saved_start':full_start,'saved_mode_switches':switches,'full_start_without_input':no_input,
            'old_saved_models_preserved':len(old),'actual_training_cases':cases,'v2_v3_training_logs_verified':telemetry,'untrained_mode_no_auto_training':True,
            'deep_profile_is_fit_budget_not_5000_ensemble_trials':True,'strict_gate_preserves_pointer':True,'patch_saved_without_input':saved,'relocated_export_without_input':relocated_start,'business_accuracy_certified':False}
    Path(a.report).write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps({'status':'PASS','training_cases':len(cases),'modes':len(switches),'full_sha256':result['full_sha256'],'patch_sha256':result['patch_sha256']}))
if __name__=='__main__':main()
