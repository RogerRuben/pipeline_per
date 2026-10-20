# -*- coding: utf-8 -*-
"""Budgeted shared/local search followed by cross-partition review and sparse fusion."""
import copy
import hashlib
import json
import math
import time
import warnings
import numpy as np
import optuna
from ..optuna_v2.optimizer_v3 import SeedIndependentTPE,LocalTrial
from .parameters import suggest,baselines,space_limits
from .metrics import ranking
from .backend import mix_evaluations
from .splits import coverage_subset


class ModelSeedTPE(SeedIndependentTPE):
    def sample_independent(self,study,trial,param_name,param_distribution):
        if param_name=='model_seed' and param_name not in self.fixed:
            return self.backup_sampler.sample_independent(study,trial,param_name,param_distribution)
        return super().sample_independent(study,trial,param_name,param_distribution)


class ModelSeedNSGA(optuna.samplers.NSGAIISampler):
    def infer_relative_search_space(self,study,trial):
        space=super().infer_relative_search_space(study,trial);space.pop('model_seed',None);return space


class ModelLocalTrial(LocalTrial):
    def suggest_int(self,name,low,high,step=1,log=False):
        if name=='model_seed':return self.trial.suggest_int(name,low,high,step=step,log=log)
        return super().suggest_int(name,low,high,step=step,log=log)


def key_of(params):return hashlib.sha256(json.dumps(params,sort_keys=True).encode('utf-8')).hexdigest()
def compact(summary):return {k:v for k,v in summary.items() if k!='fold_metrics'}


def budget_plan(cfg,count):
    s=cfg['v4']['search'];mode=s['mode'];total=cfg['search']['n_trials']
    if mode in ('random','global'):global_n=total;local=[0]*count
    elif s['budget_mode']=='per_split':global_n=s['global_trials'] if mode=='hybrid' else 0;local=[s['trials_per_split']]*count
    else:
        global_n=max(1,int(round(total*s['global_fraction']))) if mode=='hybrid' else 0
        remaining=total-global_n
        if remaining<count:raise ValueError('局部搜索预算%d不足以覆盖%d个划分；请提高n_trials、减少max_splits或选择global/random。'%(remaining,count))
        local=[remaining//count+(j<remaining%count) for j in range(count)]
    return {'global_trials':global_n,'per_split_trials':[int(x) for x in local],'total_configuration_trials':global_n+sum(local),
            'ensemble_weight_trials':cfg['v4']['ensemble']['weight_trials'] if cfg['v4']['ensemble']['enabled'] else 0,
            'budget_mode':s['budget_mode'],'requested_n_trials':total}


def run_search(backend,splits,cfg,progress):
    s=cfg['v4']['search'];plan=budget_plan(cfg,len(splits));start=time.monotonic();timeout=cfg['search']['timeout_seconds']
    bounds=(s['model_seed_low'],s['model_seed_high']) if s['model_seed_mode']=='range' else None
    seeds=[seed for seed in s['model_seeds'] if bounds is None or bounds[0]<=seed<=bounds[1]] or [bounds[0]]
    limits=space_limits(min(len(f['train']) for f in splits),s['adaptive_parameters']);warm=baselines(limits,seeds)
    records=[];done=0;studies=[];phase_counts={'nsga':0,'tpe':0};fallbacks=[]
    optuna.logging.set_verbosity(optuna.logging.WARNING)
    def expired():return timeout is not None and time.monotonic()-start>=timeout
    def constraint(trial):return (float(trial.user_attrs.get('train_violation',1e6)),)
    active_constraint=constraint if cfg['v4']['train_gate']['mode']=='hard' else None
    rng=np.random.RandomState(cfg['search']['optimizer_seed'])
    def run_context(context,folds,count,initial,random_fold=False):
        nonlocal done
        if not count or expired():return []
        offset=len(studies)*101;seed=(cfg['search']['optimizer_seed']+offset)%(2**32)
        sampler=s['sampler'];first=count if sampler=='nsga' else 0 if sampler=='tpe' else max(1,int(round(count*s['nsga_fraction'])))
        with warnings.catch_warnings():
            warnings.simplefilter('ignore',optuna.exceptions.ExperimentalWarning)
            if first:study=optuna.create_study(directions=['minimize','minimize'],sampler=ModelSeedNSGA(seed=seed,population_size=min(30,max(2,first)),constraints_func=active_constraint))
            else:study=optuna.create_study(directions=['minimize','minimize'],sampler=ModelSeedTPE(seed,5,24,constraints_func=active_constraint))
        queued=set()
        for params in initial+warm:
            fingerprint=key_of(params)
            if fingerprint not in queued:study.enqueue_trial(params);queued.add(fingerprint)
        context_records=[]
        for index in range(count):
            if expired():break
            if first and index==first:
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore',optuna.exceptions.ExperimentalWarning)
                    next_study=optuna.create_study(directions=['minimize','minimize'],sampler=ModelSeedTPE(seed,5,24,constraints_func=active_constraint))
                next_study.add_trials([t for t in study.trials if t.state in (optuna.trial.TrialState.COMPLETE,optuna.trial.TrialState.FAIL)])
                study=next_study
            phase='nsga' if first and index<first else 'tpe';trial=study.ask();trial.set_user_attr('context',context);trial.set_user_attr('phase',phase)
            anchor=None
            completed=[r for r in context_records if r['valid']]
            if phase=='tpe' and completed and index%5!=4:
                anchor=min(completed,key=lambda r:ranking(r['summary'],cfg))['parameters']
            proxy=ModelLocalTrial(trial,anchor,.25) if anchor is not None else trial
            if anchor is not None and index%4==0 and hasattr(study.sampler,'fixed'):
                study.sampler.fixed={k:v for k,v in anchor.items() if not k.startswith('weight_')}
            used=[folds[int(rng.randint(len(folds)))]] if random_fold else folds
            try:
                params=suggest(proxy,limits,s['model_seeds'],bounds);trial.set_user_attr('split_ids',[f['id'] for f in used])
                evaluated=backend.evaluate(params,used,cfg);summary=compact(evaluated['summary'])
                trial.set_user_attr('valid',True);trial.set_user_attr('train_violation',summary['train_violation'])
                train_penalty=summary['train_violation']*cfg['v4']['train_gate']['soft_penalty'] if cfg['v4']['train_gate']['mode']=='soft' else 0.
                m=summary['validation'];r2loss=math.log1p(max(0.,1.-m['r2'])) if m['r2'] is not None else 0.
                values=(math.log1p(m['mape_percent'])+train_penalty,r2loss+train_penalty)
                finished=study.tell(trial,values=values)
                record={'context':context,'phase':phase,'trial':trial.number,'valid':True,'parameters':params,'summary':summary,'error':'','split_ids':[f['id'] for f in used]}
            except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
                trial.set_user_attr('valid',False);trial.set_user_attr('error',repr(exc));finished=study.tell(trial,state=optuna.trial.TrialState.FAIL)
                record={'context':context,'phase':phase,'trial':trial.number,'valid':False,'parameters':dict(trial.params),'summary':None,'error':repr(exc),'split_ids':[f['id'] for f in used]}
            finally:
                if hasattr(study.sampler,'fixed'):study.sampler.fixed={}
            notes=getattr(study.sampler,'fallbacks_by_trial',{}).get(trial.number,[])
            if notes:fallbacks.extend(notes)
            records.append(record);context_records.append(record);done+=1;phase_counts[phase]+=1
            if record['valid']:
                m=record['summary'];progress('V4 %s %s %d/%d：训练MAPE %.3f%%、R² %s；开发MAPE %.3f%%、汇总R² %s；拟合%d、缓存%d。'%(context,phase,done,plan['total_configuration_trials'],m['train']['mape_percent'],fmt(m['train']['r2']),m['validation']['mape_percent'],fmt(m['validation']['r2']),backend.stats['model_fits'],backend.stats['prediction_cache_hits']))
            else:progress('V4 %s %d/%d：跳过不可评分候选，继续。%s'%(context,done,plan['total_configuration_trials'],record['error']))
        studies.append(study)
        return context_records
    mode=s['mode'];coarse=coverage_subset(splits,min(len(splits),max(s['coarse_splits'],int(math.ceil(len(backend.y)/len(splits[0]['validation']))))))
    if plan['global_trials']:
        search_folds=splits if mode in ('random','global') else coarse
        global_records=run_context('global',search_folds,plan['global_trials'],[],random_fold=mode=='random')
    else:global_records=[]
    global_good=sorted([r for r in global_records if r['valid']],key=lambda r:ranking(r['summary'],cfg))
    initial=[r['parameters'] for r in global_good[:s['warm_candidates']]]
    local_bests=[]
    for fold,budget in zip(splits,plan['per_split_trials']):
        context_records=run_context('split_%d'%fold['id'],[fold],budget,initial)
        good=[r for r in context_records if r['valid']]
        if good:local_bests.append(min(good,key=lambda r:ranking(r['summary'],cfg)))
    # Include shared candidates and a deterministic spread of local winners.
    # Every local winner remains in the report even if the review cap is lower.
    queue=global_good[:max(1,cfg['v4']['review']['max_candidates']//3)]
    if local_bests:
        slots=max(1,cfg['v4']['review']['max_candidates']-len(queue))
        positions=np.linspace(0,len(local_bests)-1,min(len(local_bests),slots),dtype=int)
        queue.extend(local_bests[int(j)] for j in positions)
    queue.extend(sorted([r for r in records if r['valid']],key=lambda r:ranking(r['summary'],cfg)))
    shortlisted=[];seen=set()
    for record in queue:
        fingerprint=key_of(record['parameters'])
        if fingerprint in seen:continue
        seen.add(fingerprint);shortlisted.append(record)
        if len(shortlisted)>=cfg['v4']['review']['max_candidates']:break
    if not shortlisted:
        return {'records':records,'plan':plan,'phase_counts':phase_counts,'error':'没有可评分候选。','reviewed':[],'local_bests':local_bests,'sampler_fallbacks':fallbacks}
    reviewed=[]
    for i,record in enumerate(shortlisted):
        try:
            evaluated=backend.evaluate(record['parameters'],splits,cfg);evaluated['source']=record['context'];evaluated['key']=key_of(record['parameters']);reviewed.append(evaluated)
            progress('V4跨划分复核%d/%d：%d个固定划分，开发MAPE %.3f%%。'%(i+1,len(shortlisted),len(splits),evaluated['summary']['validation']['mape_percent']))
        except (ValueError,FloatingPointError,OverflowError,RuntimeError) as exc:
            record['review_error']=repr(exc);progress('V4复核淘汰不稳定候选：'+repr(exc))
    reviewed.sort(key=lambda e:ranking(e['summary'],cfg))
    return {'records':records,'plan':plan,'phase_counts':phase_counts,'reviewed':reviewed,'local_bests':local_bests,'sampler_fallbacks':fallbacks,
            'coarse_split_ids':[f['id'] for f in coarse],'configuration_trials_completed':done,'timed_out':expired()}


def fmt(value):return '不可定义' if value is None else '%.5f'%value


def choose_ensemble(reviewed,backend,cfg,progress):
    if not reviewed:raise ValueError('所有候选在跨划分复核中均不可评分，已保存诊断，旧模型保留。')
    selected=reviewed[:cfg['v4']['ensemble']['max_configurations']] if cfg['v4']['ensemble']['enabled'] else reviewed[:1]
    count=len(selected);best=np.zeros(count);best[0]=1.;combined=mix_evaluations(selected,best,backend.y,cfg)
    if count>1:
        sampler=optuna.samplers.TPESampler(seed=cfg['search']['optimizer_seed'],n_startup_trials=min(5,cfg['v4']['ensemble']['weight_trials']))
        study=optuna.create_study(direction='minimize',sampler=sampler)
        for i in range(count):study.enqueue_trial({'configuration_%d'%j:float(i==j) for j in range(count)})
        for _ in range(cfg['v4']['ensemble']['weight_trials']):
            trial=study.ask();weights=np.asarray([trial.suggest_float('configuration_%d'%j,0.,1.) for j in range(count)])
            weights[weights<.05]=0.
            if not np.any(weights):weights[0]=1.
            weights/=weights.sum()
            try:
                current=mix_evaluations(selected,weights,backend.y,cfg);summary=current['summary']
                loss=math.log1p(summary['validation']['mape_percent'])+math.log1p(max(0.,1.-(summary['validation']['r2'] if summary['validation']['r2'] is not None else 0.)))
                if cfg['v4']['train_gate']['mode']!='off':loss+=max(summary['train_violation'],summary.get('deployed_train_violation',0.))*10.
                study.tell(trial,loss)
                reference=combined['summary'];allowed=True
                if cfg['v4']['ensemble']['non_degradation']:
                    a=summary['validation'];b=reviewed[0]['summary']['validation']
                    allowed=a['mape_percent']<=b['mape_percent']+1e-9 and (a['r2'] is not None and b['r2'] is not None and a['r2']>=b['r2']-1e-9)
                if allowed and ranking(summary,cfg)<ranking(reference,cfg):best=weights.copy();combined=current
            except (ValueError,FloatingPointError,OverflowError):study.tell(trial,state=optuna.trial.TrialState.FAIL)
    active=[i for i,w in enumerate(best) if w>0];weights=best[active];selected=[selected[i] for i in active]
    progress('V4已选择%d套参数组合；融合份额%s。'%(len(selected),','.join('%.3f'%x for x in weights)))
    return selected,weights,combined
