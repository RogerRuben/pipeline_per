# -*- coding: utf-8 -*-
"""Actual package/BAT acceptance, isolated from source imports."""
import argparse,csv,hashlib,io,json,os,subprocess,sys,zipfile
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT));sys.path.insert(0,str(ROOT/'tests'))
from product_optuna_v3_test import mixed_fixture
from tools.verify_product_v5_packages import extract
from tools.verify_product_r4_portable import exercise


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    p=argparse.ArgumentParser();p.add_argument('--full',required=True);p.add_argument('--patch',required=True);p.add_argument('--report',required=True);a=p.parse_args()
    evidence=Path(a.report).resolve().parent;full=ROOT/'outputs/v51f';patch=ROOT/'outputs/v51p';relocated=ROOT/'outputs/v51x'
    env=dict(os.environ,PRODUCT_NO_PAUSE='1',PYTHONIOENCODING='utf-8',PYTHONPATH='')
    def run(root,args,label,success=True):
        r=subprocess.run(args,cwd=str(root),env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=360,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        text=r.stdout.decode('utf-8',errors='replace');(evidence/(label+'.log')).write_text(text,encoding='utf-8')
        if success and r.returncode:raise AssertionError(label+': '+text[-7000:])
        if not success and not r.returncode:raise AssertionError(label+' unexpectedly passed')
        return text
    def py(root,args,label):return run(root,[str(root/'runtime/python38/python.exe'),'-X','utf8']+args,label)
    def bat(root,name,label,*args,**kwargs):return run(root,['cmd.exe','/d','/c',name]+list(args),label,**kwargs)
    extract(a.full,full);py(full,['tools/product_portable.py','verify','--full'],'full_manifest')
    first=exercise(full,evidence/'full_start.log',generation=True,expected_rows=16,expected_features=36,change_field='x1',change_delta=5.)
    switches=[]
    for selector in ('SELECT_V2','SELECT_V3','V4_RANDOM','V4_GLOBAL','V4_PER_SPLIT','V4_HYBRID'):
        bat(full,selector+'.bat','full_select_'+selector);status=json.loads(py(full,['tools/product_modes.py','status'],'full_status_'+selector));assert status['saved'];switches.append(selector)
    for protocol,scope in [('legacy_split','selected_split'),('fixed_cv','full_data')]:
        for mode in ('goal_first','balanced','robust'):
            py(full,['tools/product_modes.py','select','--engine','optuna_v5','--protocol',protocol,'--scope',scope,'--strategy',mode],'full_select_'+protocol+'_'+mode)
            status=json.loads(py(full,['tools/product_modes.py','status'],'full_status_'+protocol+'_'+mode));assert status['saved'];switches.append(protocol+'_'+mode)
    py(full,['tools/product_modes.py','select','--engine','optuna_v5','--protocol','fixed_cv','--scope','full_data','--strategy','goal_first'],'full_restore_default')
    (full/'input/products.xlsx').rename(full/'input/products.xlsx.disabled')
    no_input=exercise(full,evidence/'full_no_input.log',generation=False,expected_rows=16,expected_features=36,change_field='x1',change_delta=5.)
    from tools.build_product_v51 import base_archive
    extract(base_archive(),patch)
    old={p.relative_to(patch).as_posix():(sha(p),p.stat().st_mtime_ns) for p in (patch/'outputs').rglob('price_native_bundle.pkl')}
    with zipfile.ZipFile(a.patch) as z:
        info=json.loads(z.read('OPTUNA_V5_1_PATCH_FILES.json').decode('utf-8'))
        for row in info['files']:assert hashlib.sha256(z.read(row['path'])).hexdigest()==row['sha256']
        z.extractall(patch)
    bat(patch,'APPLY_V5_1.bat','patch_apply')
    assert old=={name:(sha(patch/name),(patch/name).stat().st_mtime_ns) for name in old}
    py(patch,['tools/product_portable.py','verify','--full'],'patch_manifest')
    bat(patch,'START.bat','patch_untrained','--no-browser',success=False)
    bat(patch,'APPLY_V5_1_DEEP.bat','patch_deep')
    deep=json.loads((patch/'config/price_optuna_v5.json').read_text(encoding='utf-8'))
    assert deep['search']['n_trials']==300 and deep['v5']['enhancement']['split_anchors']==1 and deep['v5']['budget']['max_member_fits']==5000
    (patch/'input/products.xlsx').rename(patch/'input/products.xlsx.disabled')
    cfg=json.loads((full/'config/price_optuna_v5.json').read_text(encoding='utf-8'));cfg['search']['n_trials']=2
    cfg['v5']['enhancement'].update(max_new_fits=8,continue_after_targets=True);cfg['v5']['persistence']['enabled']=False
    cfg['expert']['scores']={'x0':5,'x4':4};cfg['expert']['field_types']={p['parameter_id']:('enum' if p['parameter_id'] in ('x3','x4') else 'number') for p in mixed_fixture(7,40)['parameters']}
    cfgpath=patch/'config/price_optuna_v5.json'
    def write_cfg(value):cfgpath.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')
    write_cfg(cfg)
    def source(n):
        data=mixed_fixture(n,40);specs=data['parameters'];buffer=io.StringIO(newline='');w=csv.writer(buffer)
        w.writerow(['成品编号','成品名称','价格(万元)']+[p['label'] for p in specs]);w.writerow(['product_id','product_name','price_wan']+[p['parameter_id'] for p in specs])
        for row in data['agreements']:w.writerow([row['agreement_id'],'固定CV测试',row['historical_price_wan']]+[row['params'][p['parameter_id']] for p in specs])
        path=patch/'input/products.csv';path.write_bytes(buffer.getvalue().encode('utf-8-sig'))
        settings=patch/'config/portable.json';s=json.loads(settings.read_text(encoding='utf-8'));s['demo_input_sha256']=sha(path);settings.write_text(json.dumps(s,ensure_ascii=False,indent=2),encoding='utf-8')
    def current():
        pointer=json.loads((patch/'state/latest.json').read_text(encoding='utf-8'));out=patch/pointer['output'];return json.loads((out/'price/training_report.json').read_text(encoding='utf-8')),out
    cases=[]
    def train(label,n,mode='goal_first',command='RETRAIN.bat'):
        py(patch,['tools/product_modes.py','select','--engine','optuna_v5','--protocol','fixed_cv','--strategy',mode],label+'_select')
        bat(patch,'CHECK_INPUT.bat',label+'_input');text=bat(patch,command,label+'_train');r,out=current()
        assert r['protocol']=='fixed_cv' and not r['weights_fitted_on_validation'] and r['training_rows']==n
        assert r['development_metrics']['rows']==n*r['cross_split_audit']['plan']['coverage_per_row'][0]
        assert r['native_equivalence_max_delta_wan']<1e-5
        events=[json.loads(s) for s in (out/'price/training_events.jsonl').read_text(encoding='utf-8').splitlines()]
        assert {'pipeline_start','pipeline_end','search_stop','native_export_end'}.issubset({e['event'] for e in events})
        for record in [json.loads(s) for s in (out/'price/enhancement_trials.jsonl').read_text(encoding='utf-8').splitlines()] if (out/'price/enhancement_trials.jsonl').exists() else []:
            assert record['weights_before']==record['weights_after'] and record['fold_plan_sha256']==r['fold_plan_sha256']
        cases.append({'label':label,'rows':n,'mode':mode,'new_fits':r['new_member_fits'],'candidates':r['candidate_trials'],'experts':r['expert_scores_enabled'],'weights':r['weight_policy']});return r
    for n in range(4,8):source(n);train('tiny_'+str(n),n)
    for mode in ('balanced','robust'):train('mode_'+mode,7,mode)
    pure=json.loads(json.dumps(cfg));pure['expert']['enabled']=False;pure['v5']['weight_policy']='uniform_fixed';write_cfg(pure)
    r=train('uniform_no_experts',7);assert not r['expert_scores_enabled'] and r['weight_policy']=='uniform_fixed'
    resume=json.loads(json.dumps(cfg));resume['v5']['persistence']['enabled']=True;resume['v5']['enhancement']['max_new_fits']=3;write_cfg(resume)
    train('resume_first',7);resume['v5']['enhancement']['max_new_fits']=6;write_cfg(resume)
    r=train('resume_second',7,command='RESUME.bat');assert r['checkpoint_restored'] and r['new_member_fits']==6
    stopping=json.loads(json.dumps(cfg));stopping['targets'].update(r2_min=-1e6,mape_percent_max=1e6);stopping['v5']['train_gate']['mode']='off';stopping['v5']['enhancement'].update(max_new_fits=5000,continue_after_targets=False);write_cfg(stopping)
    r=train('cv_target_stop',7);assert r['new_member_fits']==0 and r['stop_reason']=='fixed_cv_targets_met'
    before=(patch/'state/latest.json').read_bytes();registry=(patch/'state/modes.json').read_bytes()
    strict=json.loads(json.dumps(cfg));strict['search']['n_trials']=1;strict['targets'].update(r2_min=1.,mape_percent_max=1e-12);strict['v5']['publish']['policy']='require_targets';strict['v5']['enhancement']['max_new_fits']=0;write_cfg(strict)
    bat(patch,'RETRAIN.bat','strict_rejection',success=False)
    assert before==(patch/'state/latest.json').read_bytes() and registry==(patch/'state/modes.json').read_bytes()
    telemetry=[]
    for version in ('v2','v3'):
        path=patch/('config/price_optuna_'+version+'.json');c=json.loads(path.read_text(encoding='utf-8'));c['search']['n_trials']=2;c['expert']['field_types']=cfg['expert']['field_types']
        path.write_text(json.dumps(c,ensure_ascii=False,indent=2),encoding='utf-8');bat(patch,'SELECT_'+version.upper()+'.bat',version+'_select')
        text=bat(patch,'RETRAIN.bat',version+'_train');assert '训练MAPE' in text and '开发MAPE' in text;telemetry.append(version)
    write_cfg(cfg);py(patch,['tools/product_modes.py','select','--engine','optuna_v5','--protocol','fixed_cv','--strategy','goal_first'],'restore_goal')
    (patch/'input/products.csv').rename(patch/'input/products.csv.disabled')
    saved=exercise(patch,evidence/'saved_no_input.log',generation=False,expected_rows=7,expected_features=40,change_field='x1',change_delta=5.)
    export=ROOT/'outputs/v51_saved_acceptance.zip'
    bat(patch,'EXPORT.bat','export_saved','--output',str(export));extract(export,relocated)
    relocated_result=exercise(relocated,evidence/'relocated_start.log',generation=False,expected_rows=7,expected_features=40,change_field='x1',change_delta=5.)
    report={'status':'PASS','full_sha256':sha(a.full),'patch_sha256':sha(a.patch),'saved_modes':switches,'old_models_preserved':len(old),
            'actual_training_cases':cases,'v2_v3_logs':telemetry,'full_start':first,'full_without_input':no_input,'saved_without_input':saved,
            'relocated_export':relocated_result,'strict_failure_preserves_pointer':True,'untrained_new_protocol_refuses_start':True,
            'weights_frozen_and_all_rows_cross_validated':True,'business_accuracy_certified':False}
    Path(a.report).write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'status':'PASS','modes':len(switches),'training_cases':len(cases),'old_models':len(old)}))


if __name__=='__main__':main()
