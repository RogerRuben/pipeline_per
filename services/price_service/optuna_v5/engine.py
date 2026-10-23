# -*- coding: utf-8 -*-
"""Preserve V3, improve fixed-prediction fusion, then spend bounded new-fit work."""
import copy
import hashlib
import json
import time
import pickle
from pathlib import Path
import numpy as np
import optuna
from ..optuna_v2 import price_automl_core as core
from ..optuna_v2.cached_backend import CachedBackend
from ..optuna_v2.typed_features import TypedBackend
from ..optuna_v2.optimizer_v3 import run_training as v3_training
from ..optuna_v4.backend import Backend as FittingBackend
from ..optuna_v4.optimizer import ModelSeedTPE
from ..optuna_v4.metrics import metrics
from .fusion import summary,rank,dominates,solve
from .persistence import BaselineObserver,atomic_json,save_state,load_state


def identity(frame,y,rows,expert,mappings,cfg):
    payload={'version':'v5-protocol-1.0','columns':list(frame.columns),'X':frame.values.tolist(),'y':np.asarray(y).tolist(),
             'rows':np.asarray(rows).tolist(),'expert':expert,'mappings':mappings,'validation':cfg['validation'],
             'core':hashlib.sha256(Path(core.__file__).read_bytes()).hexdigest(),
             'encoding':hashlib.sha256(Path(core.__file__).with_name('typed_features.py').read_bytes()).hexdigest()}
    return hashlib.sha256(json.dumps(payload,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


def split_indices(frame,y,rows,seed,cfg):
    values=core.split_data(frame,y,rows,seed,cfg['validation']['test_size'],cfg['validation']['price_bins'])
    lookup={int(row):i for i,row in enumerate(rows)}
    return np.asarray([lookup[int(row)] for row in values[4]],int),np.asarray([lookup[int(row)] for row in values[5]],int)


def bank_from_snapshot(snapshot,frame,y,rows,cfg,label='v3'):
    params=dict(snapshot['params']);tr,va=split_indices(frame,y,rows,int(params['split_seed']),cfg)
    _,a=TypedBackend.predict(snapshot,frame.iloc[tr]);_,b=TypedBackend.predict(snapshot,frame.iloc[va]);entries=[];weights=[]
    for j,name in enumerate(core.MODEL_NAMES):
        if snapshot['weights'][j]<=0:continue
        one=np.zeros(len(core.MODEL_NAMES));one[j]=1.;bundle=dict(snapshot,models={name:snapshot['models'][name]},weights=one)
        member_params=dict(params,**{'weight_'+other:float(other==name) for other in core.MODEL_NAMES})
        bundle['params']=member_params
        entries.append({'name':label+'_'+name,'family':name,'parameters':member_params,'bundle':bundle,
                        'train':np.asarray(a[name],float),'validation':np.asarray(b[name],float),'pinned':True,'baseline_family':name})
        weights.append(float(snapshot['weights'][j]))
    bank={'seed':int(params['split_seed']),'train':tr,'validation':va,'entries':entries,'weights':np.asarray(weights,float)}
    bank['summary']=bank_summary(bank,y,cfg)
    return bank


def matrices(bank):return np.column_stack([e['train'] for e in bank['entries']]),np.column_stack([e['validation'] for e in bank['entries']])
def bank_summary(bank,y,cfg):
    T,V=matrices(bank);return summary(y[bank['train']],y[bank['validation']],T.dot(bank['weights']),V.dot(bank['weights']),cfg)


def pack(bank):
    active=np.flatnonzero(np.asarray(bank['weights'])>0)
    return {'format':'price-optuna-v5-1.0','weights':[float(bank['weights'][i]) for i in active],
            'configurations':[bank['entries'][i]['bundle'] for i in active],
            'entries':[{'name':bank['entries'][i]['name'],'family':bank['entries'][i]['family'],'parameters':bank['entries'][i]['parameters']} for i in active]}


def fit_member(fitter,frame,y,params,expert):
    return fitter.fit(frame,y,params,expert)


def new_parameters(trial,family,anchor,cfg):
    p=dict(anchor);p.update({'weight_'+other:float(other==family) for other in core.MODEL_NAMES})
    e=cfg['v5']['enhancement']
    p['model_seed']=trial.suggest_int('model_seed',e['model_seed_low'],e['model_seed_high']) if family in ('gbdt','extratrees') else 0
    if family=='ridge':p['ridge_alpha']=trial.suggest_float('ridge_alpha',1e-8,1e4,log=True)
    elif family=='lasso':p['lasso_alpha']=trial.suggest_float('lasso_alpha',1e-8,1.,log=True)
    elif family=='svr':
        p['svr_kernel']=trial.suggest_categorical('svr_kernel',['linear','rbf']);p['svr_C']=trial.suggest_float('svr_C',.01,1e4,log=True)
        p['svr_epsilon']=trial.suggest_float('svr_epsilon',1e-5,.3,log=True)
        if p['svr_kernel']=='rbf':p['svr_gamma']=trial.suggest_categorical('svr_gamma',['scale','auto',.001,.01,.1,1.])
    elif family=='expert_tree':
        p['tree_max_depth']=trial.suggest_int('tree_max_depth',1,9);p['tree_min_samples_leaf']=trial.suggest_categorical('tree_min_samples_leaf',[1,2])
        p['tree_lambda_expert']=trial.suggest_float('tree_lambda_expert',0.,2.);p['tree_min_gain_split']=trial.suggest_float('tree_min_gain_split',0.,.15)
    else:
        prefix='et' if family=='extratrees' else 'gbdt'
        p[prefix+'_n_estimators']=trial.suggest_categorical(prefix+'_n_estimators',[50,100,200,300])
        p[prefix+'_max_depth']=trial.suggest_int(prefix+'_max_depth',1,12 if prefix=='et' else 5)
        p[prefix+'_min_samples_split']=trial.suggest_categorical(prefix+'_min_samples_split',[2,4,6])
        if prefix=='et':p['et_max_features']=trial.suggest_categorical('et_max_features',[.3,.5,.8,1.])
        else:p['gbdt_learning_rate']=trial.suggest_float('gbdt_learning_rate',.005,.5,log=True);p['gbdt_subsample']=trial.suggest_float('gbdt_subsample',.5,1.)
    return p


def verified_reference(directory,frame,y,rows,cfg,source_sha,progress):
    if not directory:return None
    try:
        path=Path(directory).resolve();price=path/'price' if (path/'price').is_dir() else path
        report=json.loads((price/'training_report.json').read_text(encoding='utf-8'));old=json.loads((price/'effective_config.json').read_text(encoding='utf-8'))
        from automl_pipeline.product_price_optuna_v3 import load_config as normalize_v3
        old=normalize_v3(overrides=old)
        manifest=json.loads((price.parent/'prototype_manifest.json').read_text(encoding='utf-8'))
        if cfg['v5']['evaluation']['mode']!='development':raise ValueError('固定外层留出不能复用全表旧模型')
        if report.get('engine')!='optuna_v3' or not source_sha or manifest.get('source_sha256')!=source_sha:raise ValueError('不是同一原始表的V3结果')
        for section in ('expert','input','validation','targets'):
            if old[section]!=cfg[section]:raise ValueError(section+'设置不一致')
        old_v3=copy.deepcopy(old['v3']);new_v3=copy.deepcopy(cfg['v3'])
        old_v3['warm_start'].pop('auto_history',None);new_v3['warm_start'].pop('auto_history',None)
        if old['search']!=cfg['search'] or old_v3!=new_v3:raise ValueError('V3预算/采样/历史设置不一致')
        if report['trials_completed']<cfg['search']['n_trials']:raise ValueError('旧V3轮数不足当前基线要求')
        snapshot=core.load_bundle(price/'selected_split_bundle.pkl');full=core.load_bundle(price/'price_ensemble_bundle.pkl')
        if list(snapshot.get('input_feature_names',snapshot['feature_names']))!=list(frame.columns):raise ValueError('旧V3输入特征顺序与当前表不一致')
        bank=bank_from_snapshot(snapshot,frame,y,rows,cfg)
        old_scores=report['validation_metrics'];now=bank['summary']['validation']
        if not np.isclose(now['mape_percent'],old_scores['mape']*100,rtol=1e-9,atol=1e-9) or now['r2'] is None or not np.isclose(now['r2'],old_scores['r2'],rtol=1e-9,atol=1e-9):raise ValueError('当前数据重放与旧V3成绩不一致')
        progress('V5已核验同数据V3保存结果，直接复用，基线新增fit=0。')
        return {'bank':bank,'snapshot':snapshot,'full':full,'source':'verified_saved_v3','search_fits':0,'export_fits':0,'trials_completed':report['trials_completed'],'result':report}
    except (OSError,ValueError,KeyError,TypeError,EOFError,pickle.UnpicklingError,AttributeError,ImportError) as exc:
        progress('V5旧参考未通过核验，改为完整运行V3基线：'+str(exc));return None


def run(frame,y,rows,expert,mappings,category_labels,cfg,output,checkpoint_dir,progress,source_sha=None):
    output=Path(output);v=cfg['v5'];started=time.monotonic();protocol=identity(frame,y,rows,expert,mappings,cfg)
    spec={'protocol':protocol,'search':cfg['search'],'v3':cfg['v3'],'targets':cfg['targets'],'finish':v['baseline']['finish'],'gate':v['train_gate'],
          'v3_code':hashlib.sha256(Path(core.__file__).with_name('optimizer_v3.py').read_bytes()).hexdigest()}
    baseline_id=hashlib.sha256(json.dumps(spec,sort_keys=True).encode('utf-8')).hexdigest()
    search_spec={'baseline':baseline_id,'mode':v['mode'],'fusion':v['fusion'],'enhancement':{k:x for k,x in v['enhancement'].items() if k not in ('max_new_fits','continue_after_targets')},
                 'engine_code':hashlib.sha256(Path(__file__).read_bytes()+Path(__file__).with_name('fusion.py').read_bytes()+Path(__file__).with_name('review.py').read_bytes()).hexdigest()}
    engine_id=hashlib.sha256(json.dumps(search_spec,sort_keys=True).encode('utf-8')).hexdigest();directory=Path(checkpoint_dir)/engine_id[:20]
    directory.mkdir(parents=True,exist_ok=True);checkpoint=directory/'engine'
    saved=load_state(checkpoint,engine_id) if v['persistence']['enabled'] and v['persistence']['resume'] else None
    fitter=FittingBackend(frame,y,rows,expert,tuple(mappings),category_labels,0)
    if saved is None:
        baseline_directory=Path(checkpoint_dir)/('baseline_'+baseline_id[:20])
        baseline=load_state(baseline_directory/'ready',baseline_id) if v['persistence']['enabled'] and v['persistence']['resume'] else None
        if baseline is not None:
            baseline=dict(baseline,source='verified_v5_baseline_checkpoint',search_fits=0,export_fits=0,resume_prior_fits=0,resume_prior_elapsed=0.)
            progress('V5复用已完成V3基线，基线新增fit=0。')
        if baseline is None:baseline=verified_reference(v['baseline']['reference_dir'],frame,y,rows,cfg,source_sha,progress)
        if baseline is None:
            from automl_pipeline.product_price_optuna_v3 import search_settings
            backend=CachedBackend(tuple(mappings),category_labels,cfg['v3']['cache_entries']);backend.retain_estimators=True
            observer=BaselineObserver(baseline_directory,baseline_id,v['persistence']['checkpoint_interval'],v['persistence']['resume'],v['baseline']['finish'],progress,v['train_gate'],v['persistence']['enabled'])
            progress('V5保留基线：完整V3研究%d轮；增强与复核另计预算。'%cfg['search']['n_trials'])
            result=v3_training(v3_settings=search_settings(cfg),history_identity='v5-baseline-'+protocol,model_backend=backend,observer=observer,
                data_path='v5_prepared_data',output_dir=str(output/'baseline'),n_trials=cfg['search']['n_trials'],target='price_wan',
                test_size=cfg['validation']['test_size'],price_bins=cfg['validation']['price_bins'],r2_target=cfg['targets']['r2_min'],mape_target=cfg['targets']['mape_percent_max']/100.,
                seed_low=cfg['search']['seed_low'],seed_high=cfg['search']['seed_high'],optimizer_seed=cfg['search']['optimizer_seed'],drop_first=False,remove_outliers=False,verbose=False,
                prepared_data=(frame,y,rows),expert_scores=expert,progress_callback=progress,timeout_seconds=cfg['search']['timeout_seconds'],min_samples=cfg['input']['min_samples'])
            snapshot=core.load_bundle(result['saved_validation_model']);full=core.load_bundle(result['saved_model'])
            baseline={'bank':bank_from_snapshot(snapshot,frame,y,rows,cfg),'snapshot':snapshot,'full':full,'source':'v3_completed' if result['n_trials']==cfg['search']['n_trials'] else 'v3_stopped_early',
                      'search_fits':int(backend.stats['model_fits']),'export_fits':2*len(snapshot['models']),'trials_completed':result['n_trials'],'result':result}
            baseline['resume_prior_fits']=observer.restored_fits if observer is not None else 0
            baseline['resume_prior_elapsed']=observer.elapsed_before if observer is not None else 0.
            if observer.feasible_snapshot is not None:baseline['feasible_snapshot']=observer.feasible_snapshot
            elif v['persistence']['enabled']:
                feasible=load_state(baseline_directory/'feasible_champion',baseline_id)
                if feasible:baseline['feasible_snapshot']=feasible['bundle']
        if v['persistence']['enabled']:save_state(baseline_directory/'ready',baseline,baseline_id)
        reference=baseline['bank'];bank=copy.deepcopy(reference)
        T,V=matrices(bank);bank['weights'],bank['summary'],solve_info=solve(T,V,y[bank['train']],y[bank['validation']],bank['weights'],cfg,v['fusion']['non_degradation'])
        banks=[bank];best=0;solver_calls=solve_info['solves']
        if baseline.get('feasible_snapshot') is not None:
            alternate=bank_from_snapshot(baseline['feasible_snapshot'],frame,y,rows,cfg,'feasible')
            if alternate['seed']!=bank['seed'] or alternate['entries'][0]['parameters']!=bank['entries'][0]['parameters']:
                for entry in alternate['entries']:entry.pop('baseline_family',None)
                T,V=matrices(alternate);alternate['weights'],alternate['summary'],info=solve(T,V,y[alternate['train']],y[alternate['validation']],alternate['weights'],cfg,v['fusion']['non_degradation']);solver_calls+=info['solves']
                banks.append(alternate)
                if rank(alternate['summary'],cfg)<rank(bank['summary'],cfg) and (not reference['summary']['qualified'] or dominates(alternate['summary'],reference['summary'])):best=1
        sampler=ModelSeedTPE(cfg['search']['optimizer_seed'],5,24);study=optuna.create_study(direction='minimize',sampler=sampler)
        for family in v['enhancement']['families']:study.enqueue_trial({'family':family})
        state={'baseline':baseline,'reference':reference,'banks':banks,'best':best,'study':study,'new_fits':0,'audit_fits':0,'full_fits':0,
               'plateau':0,'solver_calls':solver_calls,'elapsed':baseline.get('resume_prior_elapsed',0.),'seen':set(),'records':[],'resumed':False}
        session_before=baseline.get('resume_prior_fits',0)
    else:
        state=saved;state['resumed']=True;session_before=state['baseline']['search_fits']+state['baseline']['export_fits']+state['new_fits']+state['audit_fits']+state['full_fits'];progress('V5恢复候选库及采样状态：已新增fit=%d；原V3基线保留。'%state['new_fits'])
    def elapsed():return state['elapsed']+time.monotonic()-started
    def used():return state['baseline']['search_fits']+state['baseline']['export_fits']+state['new_fits']+state['audit_fits']+state['full_fits']
    def room(reserve=0):return v['budget']['max_member_fits'] is None or used()+reserve<v['budget']['max_member_fits']
    def checkpoint_now():
        if v['persistence']['enabled']:
            data=dict(state,elapsed=elapsed());save_state(checkpoint,data,engine_id)
        current=state['banks'][state['best']]
        atomic_json(output/'search_champion.json',{'source':'v5','seed':current['seed'],'summary':current['summary'],'new_fits':state['new_fits'],'cumulative_model_fits':used()})
        save_state(output/'search_champion',{'pack':pack(current),'train_rows':rows[current['train']],'validation_rows':rows[current['validation']]},engine_id)
    checkpoint_now();reason='new_fit_budget_exhausted';duplicate=0
    while state['new_fits']<v['enhancement']['max_new_fits']:
        current=state['banks'][state['best']]
        if v['mode']!='robust' and current['summary']['qualified'] and not v['enhancement']['continue_after_targets']:reason='targets_met_after_baseline';break
        if cfg['search']['timeout_seconds'] is not None and elapsed()>=cfg['search']['timeout_seconds']:reason='time_budget_exhausted';break
        audit_reserve=v['audit']['max_splits']*v['audit']['max_members'] if v['audit']['enabled'] or v['mode']!='goal_first' else 0
        if not room(v['enhancement']['pool_size']+audit_reserve):reason='total_fit_budget_reserved_for_export';break
        study=state['study']
        stagnant=state['plateau']>=v['enhancement']['patience']
        if stagnant:
            state['stage_switches']=state.get('stage_switches',0)+1
            forced=v['enhancement']['families'][state['stage_switches']%len(v['enhancement']['families'])]
            study.sampler.fixed={'family':forced};progress('V5局部停滞：切换模型族%s，继续使用剩余有效fit预算。'%forced)
        trial=study.ask();family=trial.suggest_categorical('family',v['enhancement']['families']);study.sampler.fixed={}
        index=state['best']
        if stagnant and v['enhancement']['split_anchors']>1:
            if len(state['banks'])<v['enhancement']['split_anchors']:
                seed=trial.suggest_int('split_seed',cfg['search']['seed_low'],cfg['search']['seed_high']);tr,va=split_indices(frame,y,rows,seed,cfg)
                state['banks'].append({'seed':seed,'train':tr,'validation':va,'entries':[],'weights':np.zeros(0),'summary':None})
                index=len(state['banks'])-1
            else:index=state['stage_switches']%len(state['banks'])
        if stagnant:state['plateau']=0
        bank=state['banks'][index];anchor=state['baseline']['snapshot']['params'];params=new_parameters(trial,family,anchor,cfg);params['split_seed']=bank['seed']
        key=json.dumps([bank['seed'],family,{k:x for k,x in params.items() if not k.startswith('weight_')}],sort_keys=True)
        if key in state['seen']:
            study.tell(trial,state=optuna.trial.TrialState.FAIL);duplicate+=1
            if duplicate>100:reason='duplicate_space_exhausted';break
            continue
        state['seen'].add(key);duplicate=0;state['new_fits']+=1;record={'number':trial.number,'family':family,'split_seed':bank['seed'],'parameters':params,'valid':False}
        entry_added=False;old_entries=list(bank['entries']);old_weights=bank['weights'].copy();old_summary=copy.deepcopy(bank['summary']);told=False
        try:
            fitted=fit_member(fitter,frame.iloc[bank['train']],y[bank['train']],params,expert)
            a,_=TypedBackend.predict(fitted,frame.iloc[bank['train']]);b,_=TypedBackend.predict(fitted,frame.iloc[bank['validation']])
            entry={'name':'fit_%d_%s'%(state['new_fits'],family),'family':family,'parameters':params,'bundle':fitted,'train':a,'validation':b,'pinned':False}
            metrics(y[bank['train']],a);metrics(y[bank['validation']],b)
            if len(bank['entries'])>=v['enhancement']['pool_size']:
                removable=[i for i,e in enumerate(bank['entries']) if not e['pinned'] and bank['weights'][i]<1e-10]
                if removable:
                    remove=removable[0];bank['entries'].pop(remove);bank['weights']=np.delete(bank['weights'],remove)
                else:
                    study.tell(trial,state=optuna.trial.TrialState.FAIL);state['plateau']+=1
                    record['error']='candidate_pool_full_with_active_members; retained incumbent'
                    state['records'].append(record)
                    with (output/'enhancement_trials.jsonl').open('a',encoding='utf-8') as handle:handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
                    if state['new_fits']%v['persistence']['checkpoint_interval']==0:checkpoint_now()
                    continue
            old=copy.deepcopy(bank['summary']);bank['entries'].append(entry);entry_added=True
            initial=np.r_[bank['weights'],0.] if len(bank['weights']) else np.ones(1)
            T,V=matrices(bank);weights,score,info=solve(T,V,y[bank['train']],y[bank['validation']],initial,cfg,v['fusion']['non_degradation'])
            state['solver_calls']+=info['solves'];bank['weights']=weights;bank['summary']=score
            best_bank=state['banks'][state['best']]
            if rank(score,cfg)<rank(best_bank['summary'],cfg) and (not state['reference']['summary']['qualified'] or dominates(score,state['reference']['summary'])):state['best']=index
            improved=old is None or rank(score,cfg)<rank(old,cfg);state['plateau']=0 if improved else state['plateau']+1
            r=score['validation']['r2'];loss=np.log1p(score['validation']['mape_percent'])+np.log1p(max(0.,1.-r if r is not None else 1.))
            if v['train_gate']['mode']!='off':loss+=10.*score['train_violation']
            individual=summary(y[bank['train']],y[bank['validation']],a,b,cfg)
            loss+=.05*np.log1p(individual['validation']['mape_percent'])
            study.tell(trial,float(loss));told=True;record.update(valid=True,summary=score,solver=info)
            progress('V5新增fit %d/%d，%s；本轮开发MAPE %.3f%%、R² %s；已保存优胜开发 %.3f%%。'%(state['new_fits'],v['enhancement']['max_new_fits'],family,score['validation']['mape_percent'],str(r),state['banks'][state['best']]['summary']['validation']['mape_percent']))
        except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
            if told:raise
            if entry_added:
                bank['entries']=old_entries;bank['weights']=old_weights;bank['summary']=old_summary
            study.tell(trial,state=optuna.trial.TrialState.FAIL);record['error']=repr(exc);state['plateau']+=1;progress('V5跳过失败成员：'+repr(exc))
        state['records'].append(record)
        with (output/'enhancement_trials.jsonl').open('a',encoding='utf-8') as handle:handle.write(json.dumps(record,ensure_ascii=False,allow_nan=False)+'\n')
        if state['new_fits']%v['persistence']['checkpoint_interval']==0 or record.get('valid') and state['plateau']==0:checkpoint_now()
    if v['enhancement']['max_new_fits']==0:reason='baseline_and_cached_fusion_only'
    checkpoint_now();selected=state['banks'][state['best']]
    return {'state':state,'selected':selected,'reference':state['reference'],'reason':reason,'elapsed_seconds':elapsed(),'identity':engine_id,
            'checkpoint_directory':str(directory),'baseline_identity':baseline_id,'used_fits':used(),'fits_before_session':session_before,'fitter':fitter,'checkpoint':checkpoint}
